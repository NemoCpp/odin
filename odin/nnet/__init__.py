from __future__ import print_function, absolute_import, division

from .model import ModelDescriptor, get_model_descriptor
from .base import *
from .conv import *
from .noise import *
from .shape import *
from .sampling import *
from .normalization import *
from .embedding import *
from .helper import *
from .rnn import *


# ===========================================================================
# Helper method for serialize NNOp
# ===========================================================================
def serialize(nnops, path, save_variables=True, variables=[],
              override=False):
    """ Serialize NNOp or list of NNOp and all necessary variables
    to a folder.

    Parameters
    ----------
    nnops: NNOp, Object, or list; tuple of NNOp and Object
    path: str
        path to a folder
    save_variables: bool
        if True, save all variables related to all given NNOps
    variables: list of tensorflow Variables
        additional list of variables to be saved with this model
    override: bool
        if True, remove existed folder to override everythin.

    Return
    ------
    path: str
        path to the folder that store NNOps and variables
    """
    # ====== checking path ====== #
    if os.path.exists(path):
        if os.path.isfile(path):
            raise ValueError("path must be path to a folder.")
        elif override:
            shutil.rmtree(path); os.mkdir(path)
    else:
        os.mkdir(path)
    nnops_path = os.path.join(path, 'nnops.ai')
    vars_path = os.path.join(path, 'variables')
    # ====== getting save data ====== #
    var = []
    if save_variables:
        var = nnops.variables if isinstance(nnops, (NNOp, ModelDescriptor)) else \
            flatten_list([o.variables for o in nnops if isinstance(o, NNOp)])
    var = list(set(var + as_list(variables)))
    # save NNOps
    with open(nnops_path, 'w') as f:
        cPickle.dump(nnops, f, protocol=cPickle.HIGHEST_PROTOCOL)
    # save Variables
    if len(var) > 0:
        K.save_variables(var, vars_path)
    return path


def deserialize(path):
    if not (os.path.exists(path) and os.path.isdir(path)):
        raise ValueError("path must be path to a folder.")
    nnops_path = os.path.join(path, 'nnops.ai')
    vars_path = os.path.join(path, 'variables')
    # ====== load the NNOps ====== #
    if not os.path.exists(nnops_path):
        raise ValueError("Cannot file path to serialized NNOps at: %s" % nnops_path)
    with open(nnops_path, 'r') as f:
        nnops = cPickle.load(f)
    # ====== load the Variables ====== #
    if os.path.exists(vars_path + '.index'):
        K.restore_variables(vars_path)
    return nnops
