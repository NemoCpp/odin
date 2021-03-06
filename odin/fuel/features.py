# -*- coding: utf-8 -*-
# ===========================================================================
# Parallel features processing using multi-core CPU and multiprocessing
# Copyright 2016-2017 TrungNT
# ===========================================================================
from __future__ import print_function, division, absolute_import

import re
import os
import sys
import wave
import time
import shutil
import warnings
from numbers import Number
from multiprocessing import Pool, cpu_count, Process, Queue
from six import add_metaclass, string_types
from six.moves import zip, zip_longest, range, cPickle
from abc import ABCMeta, abstractmethod, abstractproperty

from collections import defaultdict
import numpy as np

from odin.ml import MiniBatchPCA
from odin.utils.mpi import MPI
from odin.preprocessing import speech, video, image, signal
from odin.utils import (Progbar, as_tuple, get_all_files, ctext,
                        get_tempdir, is_string, batching,
                        add_notification, keydefaultdict)
from .dataset import Dataset
from .recipes import FeederRecipe
from .utils import MmapDict, SQLiteDict

_default_module = re.compile('__.*__')

__all__ = [
    'WaveProcessor',
    'SpeechProcessor',
]


# ===========================================================================
# Helper
# ===========================================================================
# ==================== For speech ==================== #
def _append_energy_and_deltas(s, energy, delta_order):
    # s.shape = [Time, Dimension]
    if s is None:
        return None
    if energy is not None:
        s = np.hstack((s, energy[:, None]))
    # compute delta
    if delta_order > 0:
        deltas = speech.compute_delta(s.T, order=delta_order)
        # tranpose back to [Time, Dim]
        s = np.hstack([s] + [i.T for i in deltas])
    return s


# ==================== general ==================== #
@add_metaclass(ABCMeta)
class FeatureProcessor(object):

    """ FeatureProcessor
    Following attribtues must be set for overriding this class:
    jobs: list
        list of all jobs for processing
    njobs: int
        number of jobs, if njobs is 0, then njobs = len(jobs)

    By overriding this class, these three properties must be carefully
    defined:
    _features_properties: tuple, list
        list of (name-str, dtype-dtype, save_statistics-bool),
        this list determines which features will be processed and saved.
    _external_indices: tuple, list
        Return a list of feature names that have separated indices
        file.
    _excluded_pca: list
        All feature properties with name given in this list will
        be excluded during pca calculation.
    """

    def __init__(self, output_path, datatype='memmap',
                 pca=True, pca_whiten=False,
                 save_stats=True, substitute_nan=None,
                 ncache=0.12, ncpu=1):
        super(FeatureProcessor, self).__init__()
        if datatype not in ('memmap', 'hdf5'):
            raise ValueError('datatype must be "memmap", or "hdf5"')
        self.datatype = datatype
        self.output_path = output_path
        # PCA
        self.pca = bool(pca)
        self.pca_whiten = bool(pca_whiten)
        # STATS
        self.save_stats = bool(save_stats)
        self.substitute_nan = substitute_nan
        self.ncpu = ncpu
        self.ncache = ncache
        # defaults
        self.jobs = []
        self.njobs = 0
        # ====== internal control for feature processor ====== #
        # list of (name, dtype, static-able)
        self._features_properties = []
        # list of features name
        self._external_indices = []
        self._excluded_pca = []

    # ==================== Abstract properties ==================== #
    @property
    def features_properties(self):
        """ Return list of features' properties
        [(name, dtype, statistic-able), ...]
        Note
        ----
        A statistic-able feature will also be calculated and stored
        the sum1, sum2, and PCA
        """
        if len(self._features_properties) == 0:
            raise ValueError("`_features_properties` must be defined and length>0 "
                             "when overriding FeatureProcessor.")
        for name, dtype, static_able in self._features_properties:
            if not is_string(name) or \
            not (is_string(dtype) or isinstance(dtype, np.dtype)) or \
            not (isinstance(bool(static_able), type(True))):
                raise ValueError("features_properties is list of 3 elements, "
                    "includes: name (string), dtype (string or numpy.dtype), "
                    "and statistic-able (bool), but given element is: %s" %
                    str([name, dtype, static_able]))
        return self._features_properties

    @property
    def external_indices(self):
        """Return a list of feature names that have separated indices
        file """
        for feature_name in self._external_indices:
            if not is_string(feature_name):
                raise ValueError('external_indices is list of feature_name which '
                    'is string type.')
        return self._external_indices

    @property
    def excluded_pca(self):
        """ All feature properties with name given in this list will
        be excluded during pca calculation"""
        for feature_name in self._excluded_pca:
            if not is_string(feature_name):
                raise ValueError('_excluded_pca is list of feature_name which '
                    'is string type.')
        return self._excluded_pca

    def _map_multiple_works(self, jobs):
        for j in jobs:
            for result in self.map(j):
                yield result

    @abstractmethod
    def map(self, job):
        """This function return an iterator of results"""
        pass

    @abstractmethod
    def _validate(self, ds, path, nb_samples, logger):
        """
        ds: Dataset (in read_only mode, auto opened and closed)
        """
        pass

    def validate(self, path, nb_samples=8):
        def logger(title, check):
            print(ctext('   *', 'cyan'),
                  ctext(title, 'yellow'),
                  "✓" if bool(check) else "✗")
        print(ctext('[%s]Validating dataset:' % self.__class__.__name__, 'red'),
              '"%s"' % self.output_path)
        ds = Dataset(self.output_path, read_only=True)
        path = str(path)
        nb_samples = int(nb_samples)
        if not os.path.exists(path):
            os.mkdir(path)
        elif os.path.isfile(path):
            raise ValueError("`path` must be a path to folder.")
        else:
            shutil.rmtree(path)
            os.mkdir(path)
        self._validate(ds, path, nb_samples, logger)
        ds.close()

    def run(self):
        if not hasattr(self, 'jobs'):
            raise Exception('the Processor must has "jobs" attribute, which is '
                            'the list of all jobs.')
        njobs = len(self.jobs) if self.njobs == 0 else self.njobs
        dataset = Dataset(self.output_path)
        datatype = self.datatype
        if self.ncpu is None: # auto select number of CPU
            ncpu = min(njobs, int(1.2 * cpu_count()))
        else:
            ncpu = self.ncpu
        # ====== indices ====== #
        databases = keydefaultdict(lambda name: MmapDict(
            path=os.path.join(dataset.path, name),
            cache_size=10000, read_only=False))
        # ====== statistic ====== #
        # mapping: feature_name -> able-to-calculate-statistics
        statistic_able = {name: stats_able
                          for name, dtype, stats_able in self.features_properties}
        sum1 = defaultdict(int)
        sum2 = defaultdict(int)
        # init PCA
        pca = defaultdict(lambda *args, **kwargs:
            MiniBatchPCA(n_components=None, whiten=self.pca_whiten,
                         copy=True, batch_size=None) if self.pca else None)
        # load old statistics and PCA if found
        for name, is_stats in statistic_able.iteritems():
            if is_stats:
                if name + '_sum1' in dataset:
                    sum1[name] = dataset[name + '_sum1'][:]
                if name + '_sum2' in dataset:
                    sum2[name] = dataset[name + '_sum2'][:]
                if name + '_pca' in dataset:
                    pca[name] = dataset[name + '_pca']
        # all data are cached for periodically flushed
        cache = defaultdict(list)
        if self.ncache <= 1:
            cache_limit = max(2, int(0.12 * njobs))
        else:
            cache_limit = int(self.ncache)
        # ref_vars[start]
        ref_vars = {'start': defaultdict(int), 'processed_count': 0}

        # ====== helper ====== #
        def flush_feature(name, cache_data):
            if len(cache_data) > 0:
                cache_data = np.concatenate(cache_data, 0)
                # NOTE: if nb_samples < nb_features, fitting PCA
                # will course error
                if self.pca and statistic_able[name] and \
                name not in self.excluded_pca and \
                (cache_data.ndim >= 2 and cache_data.shape[-1] > 1):
                    pca[name].partial_fit(cache_data)
                # flush data
                if name in dataset:
                    dataset[name].append(cache_data)
                else:
                    dataset[(name, datatype)] = cache_data

        # ====== repeated for each result returned ====== #
        def wrapped_reduce(result):
            name, job_count, data = result
            ref_vars['processed_count'] += 1
            # check data
            if not isinstance(data, (tuple, list)):
                data = (data,)
            saved_indices = []
            # processing
            for prop, d in zip(self.features_properties, data):
                # feature-type-name, dtype, stats-able
                feat_name, feat_type, feat_stat = prop
                # specal case: dict type
                if 'dict' in str(feat_type).lower():
                    databases[feat_name][name] = \
                        (d.tolist() if isinstance(d, np.ndarray) else d)
                    del d
                    continue
                # auto-create new indices
                if feat_name in self.external_indices:
                    ids_name = 'indices_%s' % feat_name
                else:
                    ids_name = 'indices'
                # do not save and increase the count of one indices multiple time
                if ids_name not in saved_indices:
                    databases[ids_name][name] = (ref_vars['start'][ids_name],
                                                 ref_vars['start'][ids_name] + len(d))
                    ref_vars['start'][ids_name] += len(d)
                    saved_indices.append(ids_name)
                # cache data, only if we have more than 0 sample
                if len(d) > 0:
                    cache[feat_name].append(d.astype(feat_type))
                    if self.save_stats and feat_stat: # save stats
                        sum1[feat_name] += np.sum(d, axis=0, dtype='float64')
                        sum2[feat_name] += np.sum(np.power(d, 2), axis=0, dtype='float64')
                del d
            # ====== flush cache ====== #
            if ref_vars['processed_count'] % cache_limit == 0: # 12 + 8
                for i, j in cache.iteritems():
                    flush_feature(i, j)
                cache.clear()
            # ====== update progress ====== #
            return name, job_count
        # ====== processing ====== #
        mpi = MPI(jobs=self.jobs,
                  map_func=self._map_multiple_works,
                  reduce_func=wrapped_reduce,
                  ncpu=ncpu,
                  buffer_size=min(8, max(len(self.jobs) // ncpu, 1)),
                  maximum_queue_size=ncpu * 3,
                  chunk_scheduler=True)
        prog = Progbar(target=njobs, name=self.__class__.__name__,
                       interval=0.1, print_report=True, print_summary=True)
        for name, job_count in mpi:
            prog['File'] = '%-20s' % name
            prog.add(job_count)
        # ====== end, flush the last time ====== #
        for i, j in cache.iteritems():
            flush_feature(i, j)
        cache = None
        dataset.flush()
        prog.add_notification("Flushed all data to disk")
        # ====== saving indices ====== #
        for name, db in databases.iteritems():
            db.flush(save_indices=True)
            db.close()
            prog.add_notification('Flush MmapDict "%s" to disk' %
                                  ctext(name, 'yellow'))

        # ====== save mean and std ====== #
        def save_mean_std(sum1, sum2, pca, name, dataset):
            N = dataset[name].shape[0]
            mean = sum1 / N
            std = np.sqrt(sum2 / N - mean**2)
            if self.substitute_nan is not None:
                mean = np.where(np.isnan(mean), self.substitute_nan, mean)
                std = np.where(np.isnan(std), self.substitute_nan, std)
            else:
                assert not np.any(np.isnan(mean)), 'Mean contains NaN, name: %s' % name
                assert not np.any(np.isnan(std)), 'Std contains NaN, name: %s' % name
            dataset[name + '_sum1'] = sum1
            dataset[name + '_sum2'] = sum2
            dataset[name + '_mean'] = mean
            dataset[name + '_std'] = std
            if pca is not None and pca.is_fitted:
                dataset[name + '_pca'] = pca
        # save all stats
        if self.save_stats:
            for n, d, s in self.features_properties:
                if s: # save stats
                    prog.add_notification('Saving statistics of: %s' % n)
                    s1, s2 = sum1[n], sum2[n],
                    if self.pca and n not in self.excluded_pca:
                        pca_ = pca[n]
                    else:
                        pca_ = None
                    save_mean_std(s1, s2, pca_, n, dataset)
        # ====== dataset flush() ====== #
        dataset.flush()
        dataset.close()
        # ====== saving the configuration ====== #
        config_path = os.path.join(self.output_path, 'config')
        # if found exist config, increase the count
        if os.path.exists(config_path):
            config_count = 1
            while True:
                if not os.path.exists(config_path + str(config_count)):
                    config_path = config_path + str(config_count)
                    break
                config_count += 1
        # save the new configuration
        config = MmapDict(config_path)
        config['__configuration_time__'] = time.time()
        for i in dir(self):
            if _default_module.match(i) is not None:
                continue
            j = getattr(self, i)
            if isinstance(j, (Number, string_types, bool)):
                config[i] = j
        config.flush(save_indices=True)
        config.close()
        prog.add_notification("Saved Processor configuration.")
        prog.add_notification("Closed all dataset.")


# ===========================================================================
# Speech features
# ===========================================================================
def _valid_segment_name(segments):
    for _, i in segments:
        if '.' in i[0] or ':' in i[0]:
            raise ValueError("Segment name cannot contain: '.' or ':', the given"
                " name is: %s" % i[0])


def _segments_preprocessing(segments, audio_ext):
    """ Filter segments into map of jobs
    Return
    ------
    jobs: dict
        file_name -> [segments, ...]
    nb_jobs: int
        total number of segment found
    """

    audio_ext = as_tuple('' if audio_ext is None else audio_ext,
                         t=string_types)
    # ====== load jobs ====== #
    if isinstance(segments, Dataset):
        # WAVE dataset
        if ('indices' in segments and 'raw' in segments and 'sr' in segments):
            file_list = [(name, segments, 0., -1., 0)
                         for name, (start, end) in segments['indices'].iteritems()]
        else: # assume that each key in dataset is a files
            file_list = [(os.path.basename(segments[k]), segments[k], 0.0, -1.0, 0)
                         for k in segments.keys()] # segment, path, start, end
    # NOT loaded segments
    elif isinstance(segments, str):
        if not os.path.exists(segments):
            raise ValueError('Path to segments must exists, however, '
                             'exist(segments)={}'.format(os.path.exists(segments)))
        # given a directory
        if os.path.isdir(segments):
            file_list = get_all_files(segments)
            file_list = [(os.path.basename(i), i, 0.0, -1.0)
                         for i in file_list] # segment, path, start, end
        # given csv file
        else:
            file_list = np.genfromtxt(segments, dtype=str, delimiter=' ')
    # LOADED segments
    elif isinstance(segments, (tuple, list, np.ndarray)):
        # just a list of path to file
        if isinstance(segments[0], str):
            file_list = [(os.path.basename(i), os.path.abspath(i), 0.0, -1.0)
                         for i in segments]
        # list of all information
        elif isinstance(segments[0], (tuple, list)):
            if len(segments[0]) == 1: # only path is given
                segments = [(path, path, 0., -1., 0) for path in segments]
            elif len(segments[0]) == 2: # name and path are given
                segments = [(name, path, 0., -1., 0) for name, path in segments]
            elif len(segments[0]) != 4 and len(segments[0]) != 5:
                raise Exception('segments must contain information in following order:'
                                '[name] [path] [start] [end] [channel]')
            file_list = segments
    # filter using support audio extension
    file_list = [f for f in file_list
                 if ((isinstance(f[1], str) and
                    any(ext in f[1][-len(ext):] for ext in audio_ext)) or
                 isinstance(f[1], Dataset))]
    # if no channel is provided, append the channel
    file_list = [list(f) + [0] if len(f) == 4 else f for f in file_list]
    nb_jobs = len(file_list)
    # convert into: audio_path -> list_of_segments[(name, start, end, channel), ...]
    jobs = []
    file_jobs = defaultdict(list)
    for segment, path_or_ds, start, end, channel in file_list:
        # Dataset related jobs
        if isinstance(path_or_ds, Dataset):
            jobs.append((path_or_ds, [(segment, start, end, channel)]))
        # audio files jobs
        else:
            file_jobs[path_or_ds].append(
                (segment, float(start), float(end), int(channel)))
    file_jobs = sorted(file_jobs.items(), key=lambda x: x[0])
    jobs += file_jobs
    _valid_segment_name(jobs)
    # check empty jobs
    if len(jobs) == 0:
        raise Exception('NO jobs found for processing.')
    return jobs, nb_jobs


def _load_audio(path_or_ds, segments,
                sr, sr_info={}, sr_new=None, best_resample=True,
                maxlen=None, vad_split=False, vad_split_args={},
                remove_dc_offset=True):
    """ Return iterator of (name, data, sr) """
    # directory path for Dataset
    if is_string(path_or_ds) and os.path.isdir(path_or_ds):
        path_or_ds = Dataset(path_or_ds)
    # iterate over file path
    if is_string(path_or_ds) or isinstance(path_or_ds, file):
        s, sr_orig = speech.read(path_or_ds,
                                 remove_dc_offset=remove_dc_offset)
        # check original sample rate
        if sr_orig is not None and sr is not None and sr_orig != sr:
            raise RuntimeError('Given sample rate (%d Hz) is different from '
                               'audio file sample rate (%d Hz).' %
                               (sr, sr_orig))
        # get given sr
        if sr_orig is None:
            sr_orig = sr
        # get from sr_info
        if sr_orig is None and is_string(path_or_ds):
            sr_orig = sr_info.get(path_or_ds, None)
        # still None, then exception
        if sr_orig is None:
            raise RuntimeError("Cannot acquire original sample rate from "
                               "loaded utterance, or from given arguments "
                               "of this Processor (file: '%s')." % str(path_or_ds))
        # check if audio file is not long enough, ignore it
        if len(s) < 25:
            raise RuntimeError("Audio at path: '%s' is too short, length: %f(s)"
                               % (str(path_or_ds), len(s) / sr_orig))
        # downsampling
        if sr_new is not None:
            s = speech.resample(s, sr_orig, sr_new, best_algorithm=best_resample)
            sr_orig = sr_new
        N = len(s)
    # vad_split_audio kwargs
    minimum_duration = vad_split_args.get('minimum_duration', None)
    frame_length = vad_split_args.get('frame_length', 128)
    nb_mixtures = vad_split_args.get('nb_mixtures', 3)
    threshold = vad_split_args.get('threshold', 0.6)
    # ====== cut into segments ====== #
    for name, start, end, channel in segments:
        # iterate over dataset
        if isinstance(path_or_ds, Dataset):
            st, en = path_or_ds['indices'][name]
            s = path_or_ds['raw'][st:en]
            N = len(s)
            sr_orig = path_or_ds['sr'][name]
        # start processing
        if 0. <= start < 1. and 0. < end <= 1.: # percentage
            start = int(start * N)
            end = int(np.ceil(end * N))
        else: # given the duration in second
            start = int(float(start) * sr_orig)
            end = int(N if end <= 0 else float(end) * sr_orig)
        # check maxlen
        if maxlen is not None and (end - start > maxlen * sr_orig):
            # using VAD information to split the audio
            if vad_split:
                data = s[start:end, channel] if s.ndim > 1 else s[start:end]
                data = signal.vad_split_audio(data, sr=sr_orig,
                    maximum_duration=maxlen, minimum_duration=minimum_duration,
                    frame_length=frame_length, nb_mixtures=nb_mixtures,
                    threshold=threshold)
                accum_length = np.cumsum([0] + [len(i) for i in data[:-1]])
                for st, d in zip(accum_length, data):
                    st_ = ('%f' % (st / sr_orig)).rstrip('0').rstrip('.')
                    en_ = ('%f' % ((st + len(d)) / sr_orig)).rstrip('0').rstrip('.')
                    yield (name + ":%s:%s" % (st_, en_),
                           d,
                           sr_orig)
            # just cut into small segments
            else:
                maxlen = int(maxlen * sr_orig)
                _ = list(range(start, end, maxlen)) + [end]
                for st, en in zip(_, _[1:]):
                    st_ = ('%f' % (st / sr_orig)).rstrip('0').rstrip('.')
                    en_ = ('%f' % (en / sr_orig)).rstrip('0').rstrip('.')
                    yield (name + ":%s:%s" % (st_, en_),
                           s[st:en, channel] if s.ndim > 1 else s[st:en],
                           sr_orig)
        # return normally
        else:
            yield (name,
                   s[start:end, channel] if s.ndim > 1 else s[start:end],
                   sr_orig)


class WaveProcessor(FeatureProcessor):
    """ Concatenate all Waveform data into single memmap (or HDF5) file
    with its meta-data information included in the indices

    The saved Dataset contains 3 Data:
     * "indices": MmapDict contain the mapping from file name to (start, end).
     * "raw": the big memmap contains all concatenated raw waveform.
     * "sr": MmapDict contains the mapping from file name to its sample rate.

    Parameters
    ----------
    segments : path, list
        if path, directory of all audio file, or segment csv file in
        following format (channel can be omitted), `start` and `end`
        is in second (if `start`, or `end` is smaller than 1.0, then they
        are understand as percentage)
            name                |     path             |start|end |channel
        ------------------------|----------------------|-----|----|---
        sw02001-A_000098-001156 | /path/to/sw02001.sph | 0.0 | -1 | 0
        sw02001-B_001980-002131 | /path/to/sw02001.sph | 0.0 | -1 | 1
    output_path: str
        path to output folder
    sr: int
        sample rate
    sr_info: dict
        mapping audio_file_path -> sampling_rate for each segment
        if provided.
    sr_new: int or None
        new sample rate (if you want to down or up sampling)
    best_resample: bool
        if True, use the best but slow algorithm for resampling
    maxlen: int
        maximum length of an utterances in second, if any file is longer than
        given length, it is divided into small segments and the start time and
        end time are concatenated to the name (e.g. file:0:30)
    vad_split: boolean (default: False)
        using VAD information to split the audio in most likely silence part.
    vad_split_args: dict
        kwargs for `odin.preprocessing.signal.vad_split_audio`, includes:
        (minimum_duration, frame_length, nb_mixtures, threshold)
    dtype: numpy.dtype, None, 'auto'
        if None or 'auto', keep the original dtype of audio
    ignore_error: boolean (default: False)
        if True, ignore error files during processing
    """

    def __init__(self, segments, output_path,
                sr=None, sr_info={}, sr_new=None, best_resample=True,
                audio_ext=None, pcm=False, remove_dc_offset=True,
                maxlen=None, vad_split=False, vad_split_args={},
                dtype='float16', datatype='memmap',
                ignore_error=False, ncache=0.12, ncpu=1):
        super(WaveProcessor, self).__init__(output_path=output_path,
            datatype=datatype, pca=False, pca_whiten=False,
            save_stats=False, substitute_nan=False,
            ncache=ncache, ncpu=ncpu)
        if isinstance(segments, Dataset):
            raise ValueError("WaveProcessor does not support segments as a Dataset.")
        self.maxlen = None if maxlen is None else int(maxlen)
        self.vad_split = bool(vad_split)
        self.vad_split_args = vad_split_args
        self.jobs, self.njobs = _segments_preprocessing(segments, audio_ext)
        if dtype is None or (is_string(dtype) and dtype == 'auto'):
            s, _ = speech.read(self.jobs[0][0], pcm=pcm, dtype=None)
            dtype = s.dtype
            del s
        self.sr = sr
        self.sr_info = sr_info
        self.sr_new = sr_new
        self.best_resample = bool(best_resample)
        self.dtype = dtype
        self.pcm = pcm
        self.remove_dc_offset = remove_dc_offset
        self._features_properties = [('raw', self.dtype, False),
                                     ('sr', 'dict', False),
                                     ('dtype', 'dict', False)]
        self.ignore_error = bool(ignore_error)

    def map(self, job):
        audio_path, segments = job[0] if len(job) == 1 else job
        nb_jobs = len(segments)
        try:
            # processing all segments
            ret = []
            for name, data, sr in _load_audio(audio_path, segments,
                        self.sr, self.sr_info, self.sr_new, self.best_resample,
                        self.maxlen, self.vad_split, self.vad_split_args,
                        remove_dc_offset=self.remove_dc_offset):
                ret.append([name, 0, [data, int(sr), data.dtype.str]])
            # a hack to return proper amount of processed jobs
            ret[-1][1] = nb_jobs
            # return result
            return (i for i in ret)
        except Exception as e:
            import traceback; traceback.print_exc()
            msg = '\n[Error file]: %s, [Exception]: %s\n' % (audio_path, str(e))
            if self.ignore_error:
                add_notification(msg)
            else:
                raise RuntimeError(msg)

    def _validate(self, ds, path, nb_samples, logger):
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
        from odin.visual import plot_save
        from scipy.io.wavfile import write
        # ====== checking indices ====== #
        indices = sorted([(name, start, end)
                         for name, (start, end) in ds['indices'].iteritems()],
                         key=lambda x: x[1])
        prog = Progbar(target=len(indices) // 2, interval=0.1)
        for prev, now in zip(indices, indices[1:]):
            prog.add(1)
            assert prev[2] == now[1] # non-zero length
            assert prev[2] - prev[1] > 0 # non-zero length
            assert now[2] - now[1] > 0 # non-zero length
        assert now[-1] == len(ds['raw']) # length match length of raw
        print(); logger("Checked all indices", True)
        # ====== check sample rate ====== #
        for name, _, _ in Progbar(indices, interval=0.1,
                                  print_report=True,
                                  report_func=lambda x: [('Name', x[0])],
                                  count_func=lambda x: 1):
            sr = ds['sr'][name]
            dtype = ds['dtype'][name]
            assert sr > 0 and dtype
        logger("Checked all sample rate and data type", True)
        # ====== checking raw signal ====== #
        samples = np.random.choice(
            np.arange(len(indices)), size=nb_samples, replace=False)
        saved_samples = {}
        figure_path = os.path.join(path, 'raw.pdf')
        for i, (name, start, end) in enumerate(Progbar(indices, interval=0.1,
                                                count_func=lambda x: 1,
                                                report_func=lambda x: [('Name', x[0])],
                                                print_report=True)):
            raw = ds['raw'][start:end]
            assert not np.any(np.isnan(raw)) # No NaN value
            assert not np.all(np.isclose(raw, 0.)) # not all value closed to zeros
            # saving audio
            if i in samples:
                _ = os.path.join(path, 'tmp%d.wav' % i)
                raw = raw[:].astype('float32')
                raw = (raw - raw.mean()) / raw.std()
                write(_, rate=ds['sr'][name], data=raw)
                saved_samples[name] = _
                # saving figure
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=RuntimeWarning)
                    plt.figure(figsize=(10, 4))
                    plt.plot(speech.resample(raw, ds['sr'][name], 8000,
                                             best_algorithm=False))
                    plt.title(name)
        plot_save(figure_path, dpi=80, log=False)
        logger("Checked all raw signal", True)
        for name, save_path in saved_samples.iteritems():
            logger('Saved "%s" at path: %s' % (name, save_path), True)
        logger("Checked all raw signal", True)
        logger("Saved figure at path: %s" % figure_path, True)
        logger("All reports at folder: %s" % path, True)


class SpeechProcessor(FeatureProcessor):

    ''' Extract speech features from all audio files in given directory or
    file list, then saves them to a `keras.ext.dataset.Dataset`

    Parameters
    ----------
    segments : path, list
        if path, directory of all audio file, or segment csv file in
        following format (channel can be omitted), `start` and `end` is in second
        (if `start`, or `end` is smaller than 1. then they are understand as
        percentage)
            name                |     path             |start|end |channel
        ------------------------|----------------------|-----|----|---
        sw02001-A_000098-001156 | /path/to/sw02001.sph | 0.0 | -1 | 0
        sw02001-B_001980-002131 | /path/to/sw02001.sph | 0.0 | -1 | 1
    output_path: str
        path to output folder
    sr: int
        sample rate
    sr_info: dict
        mapping audio_file_path -> sampling_rate for each segment
        if provided.
    sr_new: int or None
        new sample rate (if you want to down or up sampling)
    best_resample: bool
        if True, use the best but slow algorithm for resampling
    win: float
        window length in millisecond
    hop: float
        hop length between windows, in millisecond
    nb_melfilters: int, or None
        number of Mel bands to generate, if None, mel-filter banks features
        won't be returned
    nb_ceps: int, or None
        number of MFCCs to return, if None, mfcc coefficients won't be
        returned
    get_spec: bool
        if True, include the log-power spectrogram
    get_qspec: bool
        if True, return Q-transform coefficients
    get_phase: bool
        if True, return phase components of STFT
    get_pitch:
        if True, include the Pitch frequency (F0)
    get_vad: int, bool
        if True, include the indicators of voice activities detection.
        if int, `get_vad` is the number of Gaussian mixture components for VAD.
        by default, use 2 distribution.
    get_energy: bool
        if True, include the log energy of each frames
    get_delta: bool or int
        if True and > 0, for each features append the delta with given order-th
        (e.g. delta=2 will include: delta1 and delta2)
    fmin : float > 0 [scalar]
        lower frequency cutoff.
    fmax : float > 0 [scalar]
        upper frequency cutoff.
    preemphasis: float `(0, 1)`
        pre-emphasis coefficience
    pitch_threshold: float in `(0, 1)`
        Voice/unvoiced threshold. Default is 0.3.
    pitch_fmax: float
        maximum frequency of pitch
    pitch_algo: 'swipe', 'rapt', 'avg'
        SWIPE - A Saw-tooth Waveform Inspired Pitch Estimation.
        RAPT - a robust algorithm for pitch tracking.
        avg - apply swipe and rapt at the same time, then take average.
        Default is 'SWIPE'
    vad_smooth: int, bool
        window length to smooth the vad indices.
        If True default window length is 3.
    vad_minlen: float (in second)
        the minimum length of audio segments that can be considered
        speech.
    cqt_bins : int > 0
        Number of frequency bins for constant Q-transform, starting at `fmin`
    center : boolean
        - If `True`, the signal `y` is padded so that frame
          `D[:, t]` is centered at `y[t * hop_length]`.
        - If `False`, then `D[:, t]` begins at `y[t * hop_length]`
    power : float > 0 [scalar]
        Exponent for the magnitude spectrogram.
        e.g., 1 for energy, 2 for power, etc.
    log: bool
        if True, convert all power spectrogram to DB
    backend: 'odin', 'sptk'
        support backend for calculating the spectra
    pca: bool
        save trained PCA for each features
    pca_whiten : bool
        When True (False by default) the ``components_`` vectors are divided
        by ``n_samples`` times ``components_`` to ensure uncorrelated outputs
        with unit component-wise variances.
        Whitening will remove some information from the transformed signal
        (the relative variance scales of the components) but can sometimes
        improve the predictive accuracy of the downstream estimators by
        making data respect some hard-wired assumptions.
    maxlen: int
        maximum length of an utterances in second, if any file is longer than
        given length, it is divided into small segments and the start time and
        end time are concatenated to the name (e.g. file:0:30)
    vad_split: boolean (default: False)
        using VAD information to split the audio in most likely silence part.
    vad_split_args: dict
        kwargs for `odin.preprocessing.signal.vad_split_audio`, includes:
        (minimum_duration, frame_length, nb_mixtures, threshold)
    save_raw: bool
        if True, saving the raw signal together with all the acoustic features
    save_stats: bool
        same the first order and second order statistics, standard deviation
        of all features
    substitute_nan: bool
        if the statistics contain NaN, replace them with zero of given
        value
    dtype: 'float16', 'float32', 'float64'
        the dtype of saved features
    datatype: 'memmap', 'hdf5'
        store processed features in memmap or hdf5
    ignore_error: boolean (default: False)
        if True, ignore error files during processing
    ncache: float or int
        number of samples are kept until flush to the disk.
    ncpu: int
        number of CPU used for this task.

    Return
    ------
    spec, mspec, mfcc, pitch, vad_idx

    Example
    -------
    >>> feat = F.SpeechProcessor(datapath, output_path, audio_ext='wav', fs=8000,
    >>>                          win=0.025, hop=0.01, n_filters=40, n_ceps=13,
    >>>                          delta_order=2, energy=True, pitch_threshold=0.5,
    >>>                          get_spec=True, get_mspec=True, get_mfcc=True,
    >>>                          get_pitch=False, get_vad=True,
    >>>                          save_stats=True, substitute_nan=None,
    >>>                          dtype='float32', datatype='memmap', ncpu=4)
    >>> feat.run()
    '''

    def __init__(self, segments, output_path,
                sr=None, sr_info={}, sr_new=None, best_resample=True,
                win=0.02, hop=0.01, window='hann',
                nb_melfilters=None, nb_ceps=None,
                get_spec=True, get_qspec=False, get_phase=False,
                get_pitch=False, get_f0=False,
                get_vad=True, get_energy=False, get_delta=False,
                fmin=64, fmax=None,
                pitch_threshold=0.3, pitch_fmax=260, pitch_algo='swipe',
                vad_smooth=3, vad_minlen=0.1,
                cqt_bins=96, preemphasis=None,
                center=True, power=2, log=True, backend='odin',
                pca=True, pca_whiten=False,
                audio_ext=None,
                maxlen=None, vad_split=False, vad_split_args={},
                save_raw=False, save_stats=True, substitute_nan=None,
                dtype='float16', datatype='memmap',
                ignore_error=False, ncache=0.12, ncpu=1):
        super(SpeechProcessor, self).__init__(output_path=output_path,
            datatype=datatype, pca=pca, pca_whiten=pca_whiten,
            save_stats=save_stats, substitute_nan=substitute_nan,
            ncache=ncache, ncpu=ncpu)
        self.maxlen = None if maxlen is None else int(maxlen)
        self.vad_split = bool(vad_split)
        self.vad_split_args = vad_split_args
        self.jobs, self.njobs = _segments_preprocessing(segments, audio_ext)
        # ====== which features to get ====== #
        features_properties = []
        if save_raw:
            features_properties.append(('raw', dtype, False))
        if get_spec: features_properties.append(('spec', dtype, True))
        if get_energy: features_properties.append(('energy', dtype, True))
        if nb_melfilters is not None:
            features_properties.append(('mspec', dtype, True))
        if nb_ceps is not None:
            features_properties.append(('mfcc', dtype, True))
        if get_qspec:
            features_properties.append(('qspec', dtype, True))
            if nb_melfilters is not None:
                features_properties.append(('qmspec', dtype, True))
            if nb_ceps is not None:
                features_properties.append(('qmfcc', dtype, True))
            if get_phase: features_properties.append(('qphase', dtype, True))
        if get_phase: features_properties.append(('phase', dtype, True))
        if get_pitch: features_properties.append(('pitch', dtype, True))
        if get_f0: features_properties.append(('f0', dtype, True))
        if get_vad:
            features_properties.append(('vad', 'uint8', False))
            features_properties.append(('vadids', 'dict', False))
        # store the sample rate of each file also
        features_properties.append(('sr', 'dict', False))
        self._features_properties = features_properties
        # control FeatureProcessor behaviour
        self._external_indices = ['vadids']
        self._excluded_pca = ['energy', 'vad']
        # ====== local variable ====== #
        self.get_spec = get_spec
        self.get_pitch = get_pitch
        self.get_f0 = get_f0
        self.get_qspec = get_qspec
        self.get_phase = get_phase
        self.get_vad = get_vad
        self.get_energy = get_energy
        self.get_delta = 0 if get_delta is None else int(get_delta)
        self.save_raw = save_raw
        # ====== feature information ====== #
        self.sr = sr
        self.sr_new = sr_new
        self.sr_info = sr_info
        self.best_resample = bool(best_resample)
        self.win = win
        self.hop = hop
        self.window = window
        self.nb_melfilters = nb_melfilters
        self.nb_ceps = nb_ceps
        # constraint pitch threshold in 0-1
        self.pitch_threshold = min(max(pitch_threshold, 0.), 1.)
        self.pitch_fmax = pitch_fmax
        self.pitch_algo = pitch_algo
        self.vad_smooth = vad_smooth
        self.vad_minlen = vad_minlen
        self.cqt_bins = cqt_bins
        self.fmin = fmin
        self.fmax = fmax
        self.preemphasis = preemphasis
        self.center = center
        self.power = power
        self.log = log
        self.backend = backend
        self.ignore_error = bool(ignore_error)

    # ==================== Abstract properties ==================== #
    def map(self, job):
        '''
        Return
        ------
        [(name, spec, mspec, mfcc, pitch, vad), ...]
        '''
        audio_path, segments = job[0] if len(job) == 1 else job
        nb_jobs = len(segments)
        try:
            ret = []
            for name, data, sr_orig in _load_audio(audio_path, segments,
                                self.sr, self.sr_info, self.sr_new, self.best_resample,
                                self.maxlen, self.vad_split, self.vad_split_args):
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning)
                    features = speech.speech_features(data.ravel(), sr=sr_orig,
                        win=self.win, hop=self.hop, window=self.window,
                        nb_melfilters=self.nb_melfilters, nb_ceps=self.nb_ceps,
                        get_spec=self.get_spec, get_qspec=self.get_qspec,
                        get_phase=self.get_phase,
                        get_pitch=self.get_pitch, get_f0=self.get_f0,
                        get_vad=self.get_vad, get_energy=self.get_energy,
                        get_delta=self.get_delta,
                        pitch_threshold=self.pitch_threshold,
                        pitch_fmax=self.pitch_fmax,
                        pitch_algo=self.pitch_algo,
                        vad_smooth=self.vad_smooth, vad_minlen=self.vad_minlen,
                        cqt_bins=self.cqt_bins, fmin=self.fmin, fmax=self.fmax,
                        sr_new=None, preemphasis=self.preemphasis,
                        center=self.center, power=self.power, log=self.log,
                        return_raw=self.save_raw, backend=self.backend)
                if features is not None:
                    saved_features = []
                    found_NaN = False
                    for i in self.features_properties[:-1]:
                        feat = features[i[0]]
                        if isinstance(feat, np.ndarray) and \
                        sum(feat.shape) > 0 and np.isnan(np.min(feat)):
                            found_NaN = True
                        else:
                            saved_features.append(feat)
                    # append the sample rate
                    if found_NaN:
                        warnings.warn('Ignore segments: %s, error: NaN values' % name)
                    else:
                        saved_features.append(sr_orig if self.sr_new is None
                                              else self.sr_new)
                        ret.append([name, 0, saved_features])
                else:
                    warnings.warn('Ignore segments: %s, no features found' % name)
            # a hack to return proper amount of processed jobs
            ret[-1][1] = nb_jobs
            # return the results as a generator
            return (i for i in ret)
        except Exception as e:
            import traceback; traceback.print_exc()
            msg = '\n[Error file]: %s, [Exception]: %s\n' % (audio_path, str(e))
            if self.ignore_error:
                add_notification(msg)
            else:
                raise RuntimeError(msg)

    def _validate(self, ds, path, nb_samples, logger):
        print(ds)
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
        from odin.visual import plot_save
        from scipy.io.wavfile import write
        # ====== get all saved features ====== #
        features_check = []
        stats_check = []
        for name, dtype, stats in self.features_properties:
            pass
        # ====== checking indices ====== #
        indices = sorted([(name, start, end)
                         for name, (start, end) in ds['indices'].iteritems()],
                         key=lambda x: x[1])
        for prev, now in zip(indices, indices[1:]):
            assert prev[2] == now[1] # non-zero length
            assert prev[2] - prev[1] > 0 # non-zero length
            assert now[2] - now[1] > 0 # non-zero length
        assert now[-1] == len(ds['raw']) # length match length of raw
        logger("Checked all indices", True)
        # ====== check sample rate ====== #
        for name, _, _ in indices:
            sr = ds['sr'][name]
            assert sr > 0
        logger("Checked all sample rate and data type", True)
        logger("All reports at folder: %s" % path, True)


# ===========================================================================
# Images
# ===========================================================================
class ImageFeatures(FeederRecipe):
    """ ImageFeauters extractor
    This function take output from Feeder with SpeechFeatures recipe
    and update output dataset

    Parameters
    ----------
    image_ext: str, or list of str
        extensions of images
    grayscale: bool
        force to convert Image to grayscale or not
    crop: 4-tuple of int
         (left, upper, right, lower)
    target_size: 2-tuple of int
        desire size for image (image will be padded if the size
        mis-match)
    transpose: int, or list of int
        if a list of int is provided, will return a list of images
        <0: Do nothing
        0: PIL.Image.FLIP_LEFT_RIGHT
        1: PIL.Image.FLIP_TOP_BOTTOM
        2: PIL.Image.ROTATE_90
        3: PIL.Image.ROTATE_180
        4: PIL.Image.ROTATE_270
        5: PIL.Image.TRANSPOSE
    resample_mode: int
        0 = PIL.Image.NEAREST: use nearest neighbour
        1 = PIL.Image.LANCZOS: a high-quality downsampling filter
        2 = PIL.Image.BILINEAR: linear interpolation
        3 = PIL.Image.BICUBIC: cubic spline interpolation

    """

    def __init__(self, image_ext=None, grayscale=False,
                 crop=None, target_size=None,
                 transpose=None, resample_mode=2):
        super(ImageFeatures, self).__init__()
        self.image_ext = ('',) if image_ext is None else as_tuple(image_ext,
                                                                  t=string_types)
        self.crop = crop if crop is None else as_tuple(crop, 4, int)
        self.grayscale = bool(grayscale)
        self.target_size = target_size
        self.transpose = (-1,) if transpose is None else as_tuple(transpose, t=int)
        self.resample_mode = resample_mode

    def shape_transform(self, shape):
        """ Return the new shape that transformed by this Recipe """
        return (shape[0] * len(self.transpose),) + shape[1:]

    def preprocess_indices(self, indices):
        # filter using support audio extension
        file_list = np.array([f for f in indices
                              if any(ext in f for ext in self.image_ext)])
        return file_list

    def map(self, path):
        '''
        Return
        ------
        [(name, spec(x, sum1, sum2), # if available, otherwise None
                mspec(x, sum1, sum2), # if available, otherwise None
                mfcc(x, sum1, sum2), # if available, otherwise None
                pitch(x, sum1, sum2), # if available, otherwise None
                vad), ...]
        '''
        X = image.read(path, grayscale=self.grayscale,
                       crop=self.crop, scale=None,
                       target_size=self.target_size,
                       transpose=self.transpose,
                       resample_mode=self.resample_mode)
        if not isinstance(X, (tuple, list)):
            X = (X,)
        name = os.path.basename(path)
        ret = []
        for i, j in zip(self.transpose, X):
            ret.append(('%s,%d' % (name, i),
                        np.expand_dims(j, axis=0)))
        return ret

    def reduce(self, images):
        for img in images:
            for name, x in img:
                # contains different transpose of images
                yield (name, x)


# ===========================================================================
# Video features
# ===========================================================================
def video_features_extraction(X, boundingbox, desire_size):
    finalX = [X]
    dtype = X.dtype
    if boundingbox is not None:
        finalX = [list() for i in range(len(boundingbox[0]) // 4)]
        # travel through each frames
        for x, bound in zip(X, boundingbox):
            # get each bounding box
            for i, box in enumerate(np.reshape(bound, (-1, 4))):
                x_, y_, w_, h_ = box
                # zero area, ignore it
                if w_ == 0 or h_ == 0:
                    if desire_size is None: continue
                    tmp = np.zeros(desire_size, dtype=dtype)
                # ====== get the bounding ====== #
                else:
                    if desire_size is not None:
                        # crop in the center
                        x_ = x_ + w_ // 2 - desire_size[-2] // 2
                        w_ = desire_size[-2] # width
                        y_ = y_ + h_ // 2 - desire_size[-1] // 2
                        h_ = desire_size[-1] # height
                    tmp = x[:, x_:x_ + w_, y_:y_ + h_]
                    # if actual size smaller than desire_size
                    # perform padding with 0.
                    if tmp.shape[-2] != w_ or tmp.shape[-1] != h_:
                        _ = np.zeros(desire_size, dtype=dtype)
                        startX = int(w_ // 2 - tmp.shape[-2] / 2)
                        startY = int(h_ // 2 - tmp.shape[-1] / 2)
                        _[:, startX: startX + tmp.shape[-2],
                          startY: startY + tmp.shape[-1]] = tmp
                        tmp = _
                # add to final results
                finalX[i].append(tmp)
        # create 1 big array hold all images
        finalX = [np.asarray(x) for x in finalX]
        finalX = (finalX[0] if len(finalX) == 1
                  else np.concatenate(finalX, axis=1))
    return (finalX,
            np.sum(finalX, axis=0, dtype='float64'),
            np.sum(finalX**2, axis=0, dtype='float64'))


class VideoFeature(FeederRecipe):

    ''' Extract speech features from all audio files in given directory or
    file list, then saves them to a `keras.ext.dataset.Dataset`

    Parameters
    ----------
    segments : path, list
        if path, directory of all audio file, or segment csv file in
        following format (channel can be omitted), start and end is in second
            name                |     path             |start|end |
        ------------------------|----------------------|-----|----|
        sw02001-A_000098-001156 | /path/to/sw02001.mp4 | 0.0 | -1 |
        sw02001-B_001980-002131 | /path/to/sw02001.mp4 | 0.0 | -1 |
    size : tuple(width, height)
        desire size of the return features images
    boundingbox : None, dict
        mapping from filename to sequence of bounding box
        (region of interest), name -> [x(from left),y(from top),width,height]
        For example: if is multiple of 4, then extract multiple regions
        sw02001-A_000098-001156 ->  [[30, 40, 15, 20, .... ], ...]
        sw02001-B_001980-002131 ->  [[30, 40, 15, 20, .... ], ...]
    robust : bool
        run in robust mode, auto ignore error files

    datatype : memmap, hdf5

    Example
    -------
    '''

    def __init__(self, segments, output, size=None,
                 boundingbox=None, video_ext=None,
                 datatype='memmap', robust=True):
        super(VideoFeature, self).__init__('VideoFeature')

    def initialize(self):
        # reversed to height width for easy processing
        if self.size is not None:
            self.size = as_tuple(self.size, N=2, t=int)
        segments = self.segments
        video_ext = as_tuple('' if self.video_ext is None
                             else self.video_ext, 1, str)
        # ====== load jobs ====== #
        if isinstance(segments, str):
            if not os.path.exists(segments):
                raise ValueError('Path to segments must exists, however, '
                                 'exist(segments)={}'.format(os.path.exists(segments)))
            if os.path.isdir(segments):
                file_list = get_all_files(segments)
                file_list = [(os.path.basename(i), i, 0.0, -1.0)
                             for i in file_list] # segment, path, start, end
            else: # csv file
                file_list = np.genfromtxt(segments, dtype=str, delimiter=' ')
        elif isinstance(segments, (tuple, list)):
            if isinstance(segments[0], str): # just a list of path to file
                file_list = [(os.path.basename(i), os.path.abspath(i), 0.0, -1.0)
                             for i in segments]
            elif isinstance(segments[0], (tuple, list)):
                if len(segments[0]) != 4:
                    raise Exception('segments must contain information in following for:'
                                    '[name] [path] [start] [end]')
                file_list = segments
        # filter using support audio extension
        file_list = [f for f in file_list if any(ext in f[1] for ext in video_ext)]
        # convert into: audio_path -> segment(name, start, end, channel)
        self.jobs = defaultdict(list)
        names = []
        for segment, file, start, end in file_list:
            self.jobs[file].append((segment, float(start), float(end)))
            names.append(segment)
        self.jobs = sorted(self.jobs.items(), key=lambda x: x[0])
        # ====== load bounding box ====== #
        if self.boundingbox is not None:
            if not isinstance(self.boundingbox, dict):
                raise ValueError('Bounding box must be a dictionary')
            if set(names) != set(self.boundingbox.keys()):
                raise Exception('Segments names and boundingbox keys mismatch.')
        # ====== check output ====== #
        self.dataset = Dataset(self.output)
        self._temp_path = get_tempdir()
        print('Temporary dir created at:', self._temp_path)
        # remove old cache files
        for p in os.listdir(self._temp_path):
            os.remove(os.path.join(self._temp_path, p))

    def map_func(self, f):
        '''
        Return
        ------
        [(name, spec(x, sum1, sum2), # if available, otherwise None
                mspec(x, sum1, sum2), # if available, otherwise None
                mfcc(x, sum1, sum2), # if available, otherwise None
                pitch(x, sum1, sum2), # if available, otherwise None
                vad), ...]
        '''
        video_path, segments = f
        # read the whole video
        frames, fps = video.read(video_path)
        size = self.size
        if size is not None:
            size = (frames.shape[1],) + size
        # generating features
        features = []
        for name, start, end in segments:
            start = int(float(start) * fps)
            end = int(frames.shape[0] if end < 0 else end * fps)
            data = frames[start:end]
            # ====== check bounding box ====== #
            box = (None if self.boundingbox is None
                   else self.boundingbox[name])
            tmp = video_features_extraction(data, box, size)
            if tmp is not None:
                features.append((name,) + tmp)
            else:
                msg = 'Ignore segments: %s, error: NaN values' % name
                warnings.warn(msg)
        # return an iterator of features
        del frames
        for name, x, sum1, sum2 in features:
            path = os.path.join(self._temp_path, name)
            # save big array, because the video can be very
            # big so we don't transfer it to Queue
            f = open(path, 'w'); np.save(f, x); f.close()
            yield name, path, sum1, sum2

    def reduce_func(self, results):
        # contains (name, spec, mspec, mfcc, vad)
        dataset = self.dataset
        datatype = self.datatype

        index = []
        sum1, sum2 = 0., 0.

        n = 0
        for name, path, s1, s2 in results:
            # load big array
            f = open(path, 'r'); X = np.load(f); f.close()
            if 'frames' in dataset: dataset['frames'].append(X)
            else: dataset[('frames', datatype)] = X
            # update running statistics
            sum1 += s1; sum2 += s2; n = X.shape[0]
            # index
            index.append([name, n])
            os.remove(path)
        dataset.flush()
        return (sum1, sum2, index)

    def finalize_func(self, results):
        # contains (sum1, sum2, n)
        dataset = self.dataset
        path = dataset.path
        sum1, sum2 = 0., 0.
        n = 0
        indices = []
        for s1, s2, index in results:
            # spec
            sum1 += s1
            sum2 += s2
            # indices
            for name, size in index:
                # name, start, end
                indices.append([name, int(n), int(n + size)])
                n += size
        # ====== saving indices ====== #
        with open(os.path.join(path, 'indices.csv'), 'w') as f:
            for name, start, end in indices:
                f.write('%s %d %d\n' % (name, start, end))
        # ====== helper ====== #
        mean = sum1 / n
        std = np.sqrt(sum2 / n - mean**2)
        assert not np.any(np.isnan(mean)), 'Mean contains NaN, name:' % os.path.basename(path)
        assert not np.any(np.isnan(std)), 'Std contains NaN, name:' % os.path.basename(path)
        dataset[name + '_mean'] = mean
        dataset[name + '_std'] = std
        # ====== clean up and release cv2 ====== #
        dataset.flush()
        dataset.close()
        # remove all temp file
        if os.path.exists(self._temp_path):
            os.remove(self._temp_path)
            self._temp_path = get_tempdir()
        return path

    def __setstate__(self, states):
        self.name = states[0]
        for name, value in states[1].iteritems():
            setattr(self, name, value)

    def __getstate__(self):
        return self.name, self._arguments
