# ===========================================================================
# Using TIDIGITS dataset to predict gender (Boy, Girl, Woman, Man)
# ===========================================================================
# Saved WAV file format:
#     0) [train|test]
#     1) [m|w|b|g] (alias for man, women, boy, girl)
#     2) [age]
#     3) [dialectID]
#     4) [speakerID]
#     5) [production]
#     6) [digit_sequence]
#     => "train_g_08_17_as_a_4291815"
# ===========================================================================
from __future__ import print_function, division, absolute_import

import matplotlib
matplotlib.use('Agg')

import os
os.environ['ODIN'] = 'gpu,float32,seed=12082518'
from collections import defaultdict

import numpy as np
import tensorflow as tf

from odin import backend as K, nnet as N, fuel as F
from odin.stats import train_valid_test_split, freqcount
from odin import training
from odin.visual import print_dist, print_confusion, print_hist
from odin.utils import (get_logpath, Progbar, get_modelpath, unique_labels,
                        as_tuple_of_shape, stdio)
# ===========================================================================
# Const
# ===========================================================================
FEAT = 'mspec'
MODEL_PATH = get_modelpath('tidigit', override=True)
LOG_PATH = get_logpath('tidigit.log', override=True)
stdio(LOG_PATH)

ds = F.Dataset('/home/trung/data/tidigits', read_only=True)
indices = ds['indices'].items()
K.get_rng().shuffle(indices)
# ====== gender and single digit distribution ====== #
gender_digits = defaultdict(int)
spk = []
dia = []
age = []
for name, (start, end) in indices:
    name = name.split('_')
    if len(name[-1]) == 1:
        gender_digits[name[1] + '-' + name[-1]] += 1
        age.append(name[2])
        dia.append(name[3])
        spk.append(name[4])
gender_digits = sorted(gender_digits.items(), key=lambda x: x[0][0])
print(print_dist(gender_digits, show_number=True))
print(len(set(spk)), len(set(dia)), len(set(age)))
print(set(age))
exit()
# ====== length ====== #
length = [(end - start) / ds['sr'][_] for _, (start, end) in indices]
print(print_hist(length, bincount=30, showSummary=True, title="Duration"))
length = max(length)
# ====== genders ====== #
f_gender, genders = unique_labels([i[0] for i in indices],
                                  lambda x: x.split('_')[1], True)
train = []
test = []
for name, (start, end) in indices:
    if name.split('_')[0] == 'train':
        train.append((name, start, end))
    else:
        test.append((name, start, end))
print(print_dist(freqcount(train, key=lambda x: x[0].split('_')[1]), show_number=True))
print(print_dist(freqcount(test, key=lambda x: x[0].split('_')[1]), show_number=True))
# ===========================================================================
# SPlit dataset
# ===========================================================================
# split by speaker ID
train, valid = train_valid_test_split(train, train=0.8,
    cluster_func=None,
    idfunc=lambda x: x[0].split('_')[4],
    inc_test=False)
print("#File train:", len(train))
print("#File valid:", len(valid))
print("#File test:", len(test))

recipes = [
    F.recipes.Name2Trans(converter_func=f_gender),
    F.recipes.LabelOneHot(nb_classes=len(genders)),
    F.recipes.Sequencing(frame_length=length, hop_length=1,
                         end='pad', endmode='post', endvalue=0)
]
feeder_train = F.Feeder(ds[FEAT], indices=train, ncpu=6, batch_mode='batch')
feeder_valid = F.Feeder(ds[FEAT], indices=valid, ncpu=6, batch_mode='batch')
feeder_test = F.Feeder(ds[FEAT], indices=test, ncpu=4, batch_mode='file')

feeder_train.set_recipes(recipes)
feeder_valid.set_recipes(recipes)
feeder_test.set_recipes(recipes)
# ===========================================================================
# Create model
# ===========================================================================
X = [K.placeholder(shape=(None,) + shape[1:], dtype='float32', name='input%d' % i)
     for i, shape in enumerate(as_tuple_of_shape(feeder_train.shape))]
y = K.placeholder(shape=(None, len(genders)), name='y', dtype='float32')
print("Inputs:", X)
print("Outputs:", y)

with N.nnop_scope(ops=['Conv', 'Dense'], b_init=None, activation=K.linear,
                  pad='same'):
    with N.nnop_scope(ops=['BatchNorm'], activation=K.relu):
        f = N.Sequence([
            N.Dimshuffle(pattern=(0, 1, 2, 'x')),
            N.Conv(num_filters=32, filter_size=(7, 7)), N.BatchNorm(),
            N.Pool(pool_size=(3, 2), strides=2),
            N.Conv(num_filters=32, filter_size=(3, 3)), N.BatchNorm(),
            N.Pool(pool_size=(3, 2), strides=2),
            N.Conv(num_filters=64, filter_size=(3, 3)), N.BatchNorm(),
            N.Pool(pool_size=(3, 2), strides=2),
            N.Flatten(outdim=2),
            N.Dense(1024), N.BatchNorm(),
            N.Dense(128),
            N.Dense(512), N.BatchNorm(),
            N.Dense(len(genders))
        ], debug=True)

y_logit = f(X)
y_prob = tf.nn.softmax(y_logit)

# ====== create loss ====== #
ce = tf.losses.softmax_cross_entropy(y, logits=y_logit)
acc = K.metrics.categorical_accuracy(y_prob, y)
cm = K.metrics.confusion_matrix(y_prob, y, labels=len(genders))
# ====== params and optimizing ====== #
params = [p for p in f.parameters
         if K.role.has_roles(p, K.role.Parameter)]
print("Parameters:", params)
optz = K.optimizers.RMSProp(lr=0.0001)
updates = optz.get_updates(ce, params)
# ====== Functions ====== #
print('Building training functions ...')
f_train = K.function(X + [y], [ce, acc, optz.norm, cm], updates=updates,
                     training=True)
print('Building testing functions ...')
f_test = K.function(X + [y], [ce, acc, cm],
                    training=False)
print('Building predicting functions ...')
f_pred = K.function(X + [y], y_prob, training=False)

# ===========================================================================
# Training
# ===========================================================================
print('Start training ...')
task = training.MainLoop(batch_size=8, seed=120825, shuffle_level=2,
                         allow_rollback=True)
task.set_save(MODEL_PATH, f)
task.set_callbacks([
    training.NaNDetector(),
    training.EarlyStopGeneralizationLoss('valid', ce,
                                         threshold=5, patience=5)
])
task.set_train_task(f_train, feeder_train, epoch=25, name='train')
task.set_valid_task(f_test, feeder_valid,
                    freq=training.Timer(percentage=0.5), name='valid')
task.run()
# ===========================================================================
# Prediction
# ===========================================================================
y_true = []
y_pred = []
for outputs in Progbar(feeder_test, name="Evaluating",
                       count_func=lambda x: x[-1].shape[0]):
    name = str(outputs[0])
    idx = int(outputs[1])
    data = outputs[2:]
    if idx >= 1:
        raise ValueError("NOPE")
    y_true.append(f_gender(name))
    y_pred.append(f_pred(*data))
y_true = np.array(y_true, dtype='int32')
y_pred = np.argmax(np.array(y_pred, dtype='float32'), -1)

from sklearn.metrics import confusion_matrix, accuracy_score
print()
print("Acc:", accuracy_score(y_true, y_pred))
print("Confusion matrix:")
print(print_confusion(confusion_matrix(y_true, y_pred), genders))
print(LOG_PATH)
