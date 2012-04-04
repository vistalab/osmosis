"""

This module is used to construct and solve models of diffusion data 

"""
import os
import warnings
import itertools

import numpy as np

# We want to try importing numexpr for some array computations, but we can do
# without:
try:
    import numexpr
    has_numexpr = True
except ImportError:
    warnings.warn("Could not import numexpr")
    has_numexpr = False
    
# Import stuff for sparse matrices:
import scipy.sparse as sps
import scipy.sparse.linalg as sla

# Get stuff from sklearn, if that's available: 
try:
    # Get both the sparse version of the Lasso: 
    from sklearn.linear_model.sparse import Lasso as spLasso
    # And the dense version:
    from sklearn.linear_model import Lasso
    # Get other stuff from sklearn.linear_model:
    from sklearn.linear_model import ElasticNet, Lars, Ridge
    # Get OMP:
    from sklearn.linear_model.omp import OrthogonalMatchingPursuit as OMP
     
    has_sklearn = True

    # Make a dict with solvers to be used for choosing among them:
    sklearn_solvers = dict(Lasso=Lasso,
                           OMP=OMP,
                           ElasticNet=ElasticNet,
                           Lars=Lars)

except ImportError:
    warnings.warn("Could not import sklearn")
    has_sklearn = False    

import scipy.linalg as la
import scipy.stats as stats
from scipy.special import sph_harm
from scipy.optimize import leastsq


import dipy.reconst.dti as dti
import dipy.core.geometry as geo
import nibabel as ni

import osmosis.descriptors as desc
import osmosis.fibers as mtf
import osmosis.tensor as mtt
import osmosis.utils as mtu
import osmosis.boot as boot


# Global constants for this module:
AD = 1.5
RD = 0.5
# This converts b values from , so that it matches the units of ADC we use in
# the Stejskal/Tanner equation: 
SCALE_FACTOR = 1000

class DWI(desc.ResetMixin):
    """
    A class for representing dwi data
    """
            
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True
                 ):
        """
        Initialize a DWI object

        Parameters
        ----------
        data: str or array
            The diffusion weighted mr data provided either as a full path to a
            nifti file containing the data, or as a 4-d array.

        bvecs: str or array
            The unit vectors describing the directions of data
            acquisition. Either an 3 by n array, or a full path to a text file
            containing the 3 by n data.

        bvals: str or array
            The values of b weighting in the data acquisition. Either a 1 by n
            array, or a full path to a text file containing the values.
        
        affine: optional, 4 by 4 array
            The affine provided in the file can be overridden by explicitely
            setting this input variable. If this is left as None, one of two
            things will happen. If the 'data' input was a file-name, the affine
            will be read from that file. Otherwise, a warning will be issued
            and affine will default to np.eye(4).

        mask: optional, 3-d array
            When provided, used as a boolean mask into the data for access. 

        sub_sample: int or array of ints.
           If we want to sub-sample the DWI data on the sphere (in the bvecs),
           we can do one of two things: 
           
        1. If sub_sample is an integer, that number of random bvecs will be
           chosen from the data.

        2. If an array of indices is provided, these will serve as indices into
        the last dimension of the data and only that part of the data will be
        used

        verbose: boolean, optional.
           Whether or not to print out various messages as you go
           along. Default: True

        """
        self.verbose=verbose
        
        # All inputs are handled essentially the same. Inputs can be either
        # strings, in which case file reads are required, or arrays, in which
        # case no file reads are needed and we assign these arrays into the
        # attributes:
        for name, val in zip(['data', 'bvecs', 'bvals'],
                             [data, bvecs, bvals]): 
            if isinstance(val, str):
                exec("self.%s_file = '%s'"% (name, val))
            elif isinstance(val, np.ndarray):
                # This time we need to give it the name-space:
                exec("self.%s = val"% name, dict(self=self, val=val))
            else:
                e_s = "%s seems to be neither an array, "% name
                e_s += "nor a file-name\n"
                e_s += "The value provided was: %s" % val
                raise ValueError(e_s)

        # You might have to scale the bvalues by some factor, so that the units
        # come out correctly in the adc calculation:
        self.bvals /= scaling_factor
        
        # You can provide your own affine, if you want and that bypasses the
        # class method provided below as an auto-attr:
        if affine is not None:
            self.affine = np.matrix(affine)

        # If a mask is provided, we will use it to access the data
        if mask is not None:
            # If it's a string, assume it's the full-path to a nifti file with
            # a binary mask: 
            if isinstance(mask, str):
                mask = ni.load(mask).get_data()
            self.mask = np.array(mask, dtype=bool)

        else:
            # Spatial mask (take only the spatial dimensions):
            self.mask = np.ones(self.shape[:3], dtype=bool)

        if sub_sample is not None:
            if np.iterable(sub_sample):
                idx = sub_sample
            else:
                idx = boot.subsample(self.bvecs[:,self.b_idx].T, sub_sample)[1]

            self.b_idx = self.b_idx[idx]
            # At this point, signal will be taken according to these
            # sub-sampled indices:
            self.data = np.concatenate([self.signal,
                                        self.data[:,:,:,self.b0_idx]],-1)
            
            self.b0_idx = np.arange(len(self.b0_idx))

            self.bvecs = np.concatenate([np.zeros((3,len(self.b0_idx))),
                                        self.bvecs[:, self.b_idx]],-1)

            self.bvals = np.concatenate([np.zeros(len(self.b0_idx)),
                                         self.bvals[self.b_idx]])
            self.b_idx = np.arange(len(self.b0_idx), len(self.b0_idx) + len(idx))


    @desc.auto_attr
    def shape(self):
        """
        Get the shape of the data. If possible, don't even load it from file to
        get that. 
        """

        # It must have been in an array
        if not hasattr(self, 'data_file'):
            # No reason not to refer to it directly:
            return self.data.shape
        
        # The data is in a file, and you might not have loaded it yet:
        else:
            # No need to actually load it yet:
            return ni.load(self.data_file).get_shape()
            
    @desc.auto_attr
    def bvals(self):
        """
        If bvals were not provided as an array, read them from file
        """
        if self.verbose:
            print("Loading from file: %s"%self.bvals_file)

        return np.loadtxt(self.bvals_file)
    
    @desc.auto_attr
    def bvecs(self):
        """
        If bvecs were not provided as an array, read them from file
        """ 
        if self.verbose:
            print("Loading from file: %s"%self.bvecs_file)

        return np.loadtxt(self.bvecs_file)

    @desc.auto_attr
    def data(self):
        """
        Load the data from file
        """
        if self.verbose:
            print("Loading from file: %s"%self.data_file)

        return ni.load(self.data_file).get_data()

    @desc.auto_attr
    def affine(self):
        """
        Get the affine transformation of the data to world coordinates
        (relative to acpc)
        """
        if hasattr(self, 'data_file'):
            # This means that there might be an affine to read in from file.
            return np.matrix(ni.load(self.data_file).get_affine())
        else:
            w_s = "DWI data generated from array. Affine will be set to"
            w_s += " np.eye(4)"
            warnings.warn(w_s)
            return np.matrix(np.eye(4))

    @desc.auto_attr
    def _flat_data(self):
        """
        Get the flat data only in the mask
        """        
        return self.data[self.mask]
               
    @desc.auto_attr
    def _flat_S0(self):
        """
        Get the signal in the b0 scans in flattened form (only in the mask)
        """
        return np.mean(self._flat_data[:,self.b0_idx], -1)


    @desc.auto_attr
    def _flat_signal(self):
        """
        Get the signal in the diffusion-weighted volumes in flattened form
        (only in the mask).
        """
        return self._flat_data[:,self.b_idx]


    def signal_reliability(self,
                           DWI2,
                           correlator=stats.pearsonr,
                           r_idx=0,
                           square=True):
        """
        Calculate the r-squared of the correlator function provided, in each
        voxel (across directions, not including b0) between this class instance
        and another class instance, provided as input.

        Parameters
        ----------
        DWI2: Another DWI class instance, with data that should look the same,
            if there wasn't any noise in the measurement

        correlator: callable. This is a function that calculates a measure of
             correlation (e.g. stats.pearsonr, or stats.linregress)

        r_idx: int,
            points to the location of r within the tuple returned by
            the correlator callable if r_idx is negative, that means that the
            return value is not a tuple and should be treated as the value
            itself.

        square: bool,
            If square is True, that means that the value returned from
            the correlator should be squared before returning it, otherwise,
            the value itself is returned.
            
        """
                
        val = np.empty(self._flat_signal.shape[0])

        for ii in xrange(len(val)):
            if r_idx>=0:
                val[ii] = correlator(self._flat_signal[ii],
                                     DWI2._flat_signal[ii])[r_idx] 
            else:
                val[ii] = correlator(self._flat_signal[ii],
                                     DWI2._flat_signal[ii]) 

        if square:
            if has_numexpr:
                r_squared = numexpr.evaluate('val**2')
            else:
                r_squared = val**2
        else:
            r_squared = val
        
        # Re-package it into a volume:
        out = np.nan*np.ones(self.shape[:3])
        out[self.mask] = r_squared

        out[out<-1]=-1.0
        out[out>1]=1.0

        return out 

    @desc.auto_attr
    def b_idx(self):
        """
        The indices into non-zero b values
        """
        return np.where(self.bvals > 0)[0]
        
    @desc.auto_attr
    def b0_idx(self):
        """
        The indices into zero b values
        """
        return np.where(self.bvals==0)[0]

    @desc.auto_attr
    def S0(self):
        """
        Extract and average the signal for volumes in which no b weighting was
        used (b0 scans)
        """
        return np.mean(self.data[...,self.b0_idx],-1).squeeze()
        
    @desc.auto_attr
    def signal(self):
        """
        The signal in b-weighted volumes
        """
        return self.data[...,self.b_idx].squeeze()

    @desc.auto_attr
    def signal_attenuation(self):
        """
        The signal attenuation in each b-weighted volume, relative to the mean
        of the non b-weighted volumes
        """
        # Need to broadcast for this to work:
        return self.signal/np.reshape(self.S0, (self.S0.shape + (1,)))

    @desc.auto_attr
    def _flat_signal_attenuation(self):
        """
        Get the flat signal attenuation only in the mask
        """        
        return np.reshape(self.signal_attenuation[self.mask],
                          (-1, self.b_idx.shape[0]))


class BaseModel(DWI):
    """
    Base-class for models.
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True):
        """
        A base-class for models based on DWI data.

        Parameters
        ----------

        scaling_factor: int, defaults to 1000.
           To get the units in the S/T equation right, how much do we need to
           scale the bvalues provided.
        
        """
        # DWI should already have everything we need: 
        DWI.__init__(self,
                         data,
                         bvecs,
                         bvals,
                         affine=affine,
                         mask=mask,
                         scaling_factor=scaling_factor,
                         sub_sample=sub_sample,
                         verbose=verbose) 

    @desc.auto_attr
    def fit(self):
        """
        Each model will have a model prediction, which is always in this class
        method. This prediction is used in other methods, such as 'residuals'
        and 'r_squared', etc.

        In this particular case, we set fit to be exactly equal to the
        signal. This should make testing easy :-) 
        """
        return self.signal

    @desc.auto_attr
    def _flat_fit(self):
        """
        Extract a flattened version of the fit, defined for masked voxels
        """
        
        return self.fit[self.mask].reshape((-1, self.signal.shape[-1])) 
        

    def _correlator(self, correlator, r_idx=0, square=True):
        """
        Helper function that uses a callable "func" to apply between two 1-d
        arrays. These 1-d arrays can have different outputs and the one we
        always want is the one which is r_idx into the output tuple 
        """

        val = np.empty(self._flat_signal.shape[0])

        for ii in xrange(len(val)):
            if r_idx>=0:
                val[ii] = correlator(self._flat_signal[ii],
                                     self._flat_fit[ii])[r_idx] 
            else:
                val[ii] = correlator(self._flat_signal[ii],
                                     self._flat_fit[ii]) 
        if square:
            if has_numexpr:
                r_squared = numexpr.evaluate('val**2')
            else:
                r_squared = val**2
        else:
            r_squared = val
        
        # Re-package it into a volume:
        out = np.nan*np.ones(self.shape[:3])
        out[self.mask] = r_squared

        out[out<-1]=-1.0
        out[out>1]=1.0

        return out 

    @desc.auto_attr
    def r_squared(self):
        """
        The r-squared ('explained variance') value in each voxel
        """
        return self._correlator(stats.pearsonr, r_idx=0)
    
    @desc.auto_attr
    def R_squared(self):
        """
        The R-squared ('coefficient of determination' from a linear model fit)
        in each voxel
        """
        return self._correlator(stats.linregress, r_idx=2)

    @desc.auto_attr
    def coeff_of_determination(self):
        """
        Explained variance as: 100 *(1-\frac{RMS(residuals)}{RMS(signal)})

        http://en.wikipedia.org/wiki/Coefficient_of_determination
        
        """
        return self._correlator(mtu.coeff_of_determination,
                                r_idx=-1,
                                square=False)

    @desc.auto_attr
    def RMSE(self):
        """
        The square-root of the mean of the squared residuals
        """

        # Preallocate the output: 
        out = np.nan*np.ones(self.data.shape[:3])

        res = self.residuals[self.mask]
        
        if has_numexpr:
            out[self.mask] = np.sqrt(np.mean(
                             numexpr.evaluate('res ** 2'), -1))
        else:
            out[self.mask] = np.sqrt(np.mean(np.power(res, 2), -1))
        
        return out

    
    @desc.auto_attr
    def residuals(self):
        """
        The prediction-subtracted residual in each voxel
        """
        out = np.nan*np.ones(self.signal.shape)
        sig = self._flat_signal
        fit = self._flat_fit
        
        if has_numexpr:
            out[self.mask] = numexpr.evaluate('sig - fit')

        else:
            out[self.mask] = sig - fit
            
        return out


# The following is a pattern used by many different classes, so we encapsulate
# it in one general function that everyone can use (DRY!):
def params_file_resolver(object, file_name_root, params_file=None):
    """
    Helper fiunction for resolving what the params file name should be for
    several of the model functions for which the params are cached to file

    Parameters
    ----------
    object: the class instance affected by this

    file_name_root: str, the string which will typically be added to the
        file-name of the object's data file in generating the model params file. 

    params_file: str or None
       If a string is provided, this will be treated as the full path to where
       the params file will get saved. This will be defined if the user
       provides this as an input to the class constructor.

    Returns
    -------
    params_file: str, full path to where the params file will eventually be
            saved, once parameter fitting is done.
    
    """
    # If the user provided
    if params_file is not None: 
        return params_file
    else:
        # If our DWI super-object has a file-name, construct a file-name out of
        # that:
        if hasattr(object, 'data_file'):
            path, f = os.path.split(object.data_file)
            # Need to deal with the double-extension in '.nii.gz':
            file_parts = f.split('.')
            name = file_parts[0]
            extension = ''
            for x in file_parts[1:]:
                extension = extension + '.' + x
                params_file = os.path.join(path, name +
                                           file_name_root +
                    extension)
        else:
            # Otherwise give up and make a file right here with a generic
            # name: 
            params_file = '%s.nii.gz'%file_name_root

    return params_file

class TensorModel(BaseModel):

    """
    A class for representing and solving a simple forward model. Just the
    diffusion tensor.
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True):
        """
        Parameters
        -----------

        data, bvecs, bvals: see DWI inputs

        scaling_factor: This scales the b value for the Stejskal/Tanner
        equation

        mask: ndarray or file-name
              An array of the same shape as the data, containing a binary mask
              pointing to the locations of voxels that should be analyzed.

        sub_sample: int or array of ints.

           If we want to sub-sample the DWI data on the sphere (in the bvecs),
           we can do one of two things: 

           1. If sub_sample is an integer, that number of random bvecs will be
           chosen from the data.

           2. If an array of indices is provided, these will serve as indices
           into the last dimension of the data and only that part of the data
           will be used


        params_file: A file to cache the initial tensor calculation in. If this
        file already exists, we pull the tensor fit out of it. Otherwise, we
        calculate the tensor fit and save this file with the params of the
        tensor fit. 
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                           data,
                           bvecs,
                           bvals,
                           affine=None,
                           mask=mask,
                           scaling_factor=scaling_factor,
                           sub_sample=sub_sample,
                           verbose=verbose) 

        self.params_file = params_file_resolver(self,
                                                'TensorModel',
                                                 params_file=params_file)
        
    @desc.auto_attr
    def model_params(self):
        """
        The diffusion tensor parameters estimated from the data, using dipy.
        If this calculation has already occurred, just load the data from a
        nifti file, which has shape x by y by z by 12, where the last dimension
        is the model params:

        evecs (9) + evals (3)
        
        """
        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading TensorModel params from: %s" %self.params_file)
            return ni.load(self.params_file).get_data()
        else:
            if self.verbose:
                print("Fitting TensorModel params using dipy")
            block = np.nan * np.ones(self.shape[:3] + (12,))
            mp = dti.Tensor(self.data,
                            self.bvals,
                            self.bvecs.T,
                            self.mask).model_params 

            # Make sure it has the right shape (this is necessary because dipy
            # reshapes things under the hood with its masked interface):
            block[self.mask] = np.reshape(mp,(-1,12))
            
            # Save the params for future use: 
            params_ni = ni.Nifti1Image(block, self.affine)
            params_ni.to_filename(self.params_file)
            # And return the params for current use:
            return block

    @desc.auto_attr
    def evecs(self):
        return np.reshape(self.model_params[..., 3:], 
                          self.model_params.shape[:3] + (3,3))

    @desc.auto_attr
    def evals(self):
        return self.model_params[..., :3]

    @desc.auto_attr
    def mean_diffusivity(self):
        #adc/md = (ev1+ev2+ev3)/3
        return self.evals.mean(-1)

    @desc.auto_attr
    def signal_adc(self):
        """
        This is the empirically defined ADC:

        .. math::
        
            ADC = -log \frac{S}{S0}

        """
        out = np.nan * np.ones(self.signal.shape)        
        out[self.mask] = ((-1/self.bvals[self.b_idx][0]) *
                        np.log(self._flat_signal_attenuation))

        return out
        
    @desc.auto_attr
    def adc_residuals(self):
        """
        The model-predicted ADC, conpared with the empirical ADC.
        """
        return self.model_adc - self.signal_adc
    
    @desc.auto_attr
    def fractional_anisotropy(self):
        """
        .. math::

            FA = \sqrt{\frac{1}{2}\frac{(\lambda_1-\lambda_2)^2+(\lambda_1-
                        \lambda_3)^2+(\lambda_2-lambda_3)^2}{\lambda_1^2+
                        \lambda_2^2+\lambda_3^2} }

        """
        out = np.nan * np.ones(self.data.shape[:3])
        
        lambda_1 = self.evals[..., 0][self.mask]
        lambda_2 = self.evals[..., 1][self.mask]
        lambda_3 = self.evals[..., 2][self.mask]

        out[self.mask] = mtu.fractional_anisotropy(lambda_1, lambda_2, lambda_3)

        return out

    @desc.auto_attr
    def radial_diffusivity(self):
        return np.mean(self.evals[...,1:],-1)

    @desc.auto_attr
    def axial_diffusivity(self):
        return self.evals[...,0]


    @desc.auto_attr
    def linearity(self):
        out = np.nan * np.ones(self.data.shape[:3])
        out[self.mask] = mtu.tensor_linearity(self.evals[..., 0][self.mask],
                                              self.evals[..., 1][self.mask],
                                              self.evals[..., 2][self.mask])
        return out

    @desc.auto_attr
    def planarity(self):
        out = np.nan * np.ones(self.data.shape[:3])
        out[self.mask] = mtu.tensor_planarity(self.evals[..., 0][self.mask],
                                              self.evals[..., 1][self.mask],
                                              self.evals[..., 2][self.mask])
        return out

    @desc.auto_attr
    def sphericity(self):
        out = np.nan * np.ones(self.data.shape[:3])
        out[self.mask] = mtu.tensor_sphericity(self.evals[..., 0][self.mask],
                                               self.evals[..., 1][self.mask],
                                               self.evals[..., 2][self.mask])
        return out

    # Self Diffusion Tensor, taken from dipy.reconst.dti:
    @desc.auto_attr
    def tensors(self):
        out = np.nan * np.ones(self.evecs.shape)
        evals = self.evals[self.mask]
        evecs = self.evecs[self.mask]
        D_flat = np.empty(evecs.shape)
        for ii in xrange(len(D_flat)):
            Q = evecs[ii]
            L = evals[ii]
            D_flat[ii] = np.dot(Q*L, Q.T)

        out[self.mask] = D_flat
        return out

    @desc.auto_attr
    def model_adc(self):
        out = np.empty(self.signal.shape)
        tensors_flat = self.tensors[self.mask].reshape((-1,3,3))
        adc_flat = np.empty(self.signal[self.mask].shape)

        for ii in xrange(len(adc_flat)):
            adc_flat[ii] = mtt.apparent_diffusion_coef(
                                        self.bvecs[:,self.b_idx],
                                        tensors_flat[ii])

        out[self.mask] = adc_flat
        return out

    @desc.auto_attr
    def fit(self):
        if self.verbose:
            print("Predicting signal from TensorModel")
        adc_flat = self.model_adc[self.mask]
        fit_flat = np.empty(adc_flat.shape)
        out = np.empty(self.signal.shape)

        for ii in xrange(len(fit_flat)):
            fit_flat[ii] = mtt.stejskal_tanner(self._flat_S0[ii],
                                               self.bvals[:, self.b_idx],
                                               adc_flat[ii])

        out[self.mask] = fit_flat
        return out

def _dyad_stats(tensor_model_list, mask=None, dyad_stat=boot.dyad_coherence,
                average=True):
    """
    Helper function that does most of the work on calcualting dyad statistics
    """
    if mask is None:
        mask = np.ones(tensor_model_list[0].shape[:3])
        
    # flatten the eigenvectors:
    tensor_model_flat=np.array([this.evecs[mask] for this in
    tensor_model_list])
    out_flat = np.empty(tensor_model_flat[0].shape[0])

    # Loop over voxels
    for idx in xrange(tensor_model_flat.shape[1]):
        dyad = boot.dyadic_tensor(tensor_model_flat[:,idx,:,:],
                                  average=average)
        out_flat[idx] = dyad_stat(dyad)

    out = np.nan * np.ones(tensor_model_list[0].shape[:3])
    out[mask] = out_flat
    return out        
    

def tensor_coherence(tensor_model_list, mask=None):
    """
    Calculate the coherence of the principle diffusion direction over a bunch
    of TensorModel class instances.


    This is $\kappa = 1-\sqrt{\frac{\beta_2 + \beta_3}{\beta_1}}$, where the
    $\beta_i$ are the eigen-values of the dyadic tensor over the list of
    TensorModel class instances provided as input. 
    """
    # This is the default behavior:
    return _dyad_stats(tensor_model_list, mask=mask,
                       dyad_stat=boot.dyad_coherence,
                       average=True)

def tensor_dispersion(tensor_model_list, mask=None):
    """
    Calculate the dispersion (in degrees) of the principle diffusion direction
    over a sample of TensorModel class instances.
    """
    # Calculate the dyad_dispersion instead:
    return _dyad_stats(tensor_model_list, mask=mask,
                       dyad_stat=boot.dyad_dispersion,
                       average=False) # This one needs to know the individual
                                      # dyads, in addition to the average one.


class SphericalHarmonicsModel(BaseModel):
    """
    A class for evaluating spherical harmonic models. This assumes that a CSD
    model was already fit somehow. Presumably by using mrtrix    
    """ 
    
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 model_coeffs,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True):
        """
        Initialize a SphericalHarmonicsModel class instance.
        
        Parameters
        ----------
        DWI: osmosis.dwi.DWI class instance.

        model_coefficients: ndarray
           Coefficients for a SH model, organized according to the conventions
           used by mrtrix (see sph_harm_set for details).
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                           data,
                           bvecs,
                           bvals,
                           affine=affine,
                           mask=mask,
                           scaling_factor=scaling_factor,
                           sub_sample=sub_sample,
                           verbose=verbose) 

        # If it's a string, assume it's a full path to a nifti file: 
        if isinstance(model_coeffs,str):
            self.model_coeffs = ni.load(model_coeffs).get_data()
        else:
            # Otherwise, it had better be an array:
            self.model_coeffs = model_coeffs

        self.L = self._calculate_L(self.model_coeffs.shape[-1])
        self.n_params = self.model_coeffs.shape[-1]

        self.ad = axial_diffusivity
        self.rd = radial_diffusivity
        
    @desc.auto_attr
    def sph_harm_set(self):
        """
        Calculate the spherical harmonics, provided n parameters (corresponding
        to nc = (L+1) * (L+2)/2 with L being the maximal harmonic degree for
        the set of bvecs of the object

        Note
        ----

        1. This was written according to the documentation of mrtrix's
        'csdeconv'. The following is taken from there:  

          Note that this program makes use of implied symmetries in the
          diffusion profile. First, the fact the signal attenuation profile is
          real implies that it has conjugate symmetry, i.e. Y(l,-m) = Y(l,m)*
          (where * denotes the complex conjugate). Second, the diffusion
          profile should be antipodally symmetric (i.e. S(x) = S(-x)), implying
          that all odd l components should be zero. Therefore, this program
          only computes the even elements.

          Note that the spherical harmonics equations used here differ slightly
          from those conventionally used, in that the (-1)^m factor has been
          omitted. This should be taken into account in all subsequent
          calculations.

          Each volume in the output image corresponds to a different spherical
          harmonic component, according to the following convention: [0]    
          Y(0,0)  [1] Im {Y(2,2)} [2] Im {Y(2,1)} [3]     Y(2,0) [4] Re
          {Y(2,1)} [5] Re {Y(2,2)}  [6] Im {Y(4,4)} [7] Im {Y(4,3)} etc... 

          
        2. Take heed that it seems that scipy's sph_harm actually has the
        order/degree in reverse order than the convention used by mrtrix, so
        that needs to be taken into account in the calculation below

        """
                
        # Convert to spherical coordinates:
        r,theta,phi = geo.cart2sphere(self.bvecs[0, self.b_idx],
                                      self.bvecs[1, self.b_idx],
                                      self.bvecs[2, self.b_idx])

        # Preallocate:
        b = np.empty((self.model_coeffs.shape[-1], theta.shape[0]))
    
        i = 0;
        # Only even order are taken:
        for order in np.arange(0, self.L + 1, 2): # Go to L, inclusive!
           for degree in np.arange(-order,order+1):
                # In negative degrees, take the imaginary part: 
                if degree < 0:  
                    b[i,:] = np.imag(sph_harm(-1 * degree, order, theta, phi));
                else:
                    b[i,:] = np.real(sph_harm(degree, order, theta, phi));
                i = i+1;
        return b

    @desc.auto_attr
    def odf(self): 
        """        
        Generate a volume with dimensions (x,y,z, n_bvecs) where each voxel has:

        .. math::

          \sum{w_i, b_i}

        Where $b_i$ are the basis set functions defined from the spherical
        harmonics and $w_i$ are the model coefficients estimated with CSD.

        This a unit-less estimate of the orientation distribution function,
        based on the estimation of the SH coefficients. This needs to be
        convolved with a "response function", a canonical tensor, to calculate
        back the estimated signal. 
        """
        volshape = self.model_coeffs.shape[:3] # Disregarding the params
                                               # dimension
        n_vox = np.sum(self.mask) # These are the voxels we'll look at
        n_weights = self.model_coeffs.shape[3]  # This is the params dimension 

        # Reshape it so that we can multiply for all voxels in one fell swoop:
        d = np.reshape(self.model_coeffs[self.mask], (n_vox, n_weights))

        out = np.empty(self.signal.shape)
        
        # multiply these two matrices together for the estimated odf:  
        out[self.mask] = np.asarray(np.matrix(d) *
                                     np.matrix(self.sph_harm_set))

        return out 
    

    @desc.auto_attr
    def response_function(self):
        """
        A canonical tensor that describes the presumed response of a single
        fiber 
        """
        return mtt.Tensor(np.diag([self.ad, self.rd, self.rd]),
                          self.bvecs[:,self.b_idx],
                          self.bvals[self.b_idx])
        

    @desc.auto_attr
    def fit(self):
        """
        This is the signal estimated from the odf.
        """
        if self.verbose:
            print("Predicting signal from SphericalHarmonicsModel")

        # Reshape the odf to be one voxel per row:
        flat_odf = self.odf[self.mask]
        pred_sig = np.empty(flat_odf.shape)

        for vox in range(pred_sig.shape[0]):
            pred_sig[vox] = self.response_function.convolve_odf(
                                                    flat_odf[vox],
                                                    self._flat_S0[vox])

        # Pack it back into a volume shaped thing: 
        out = np.ones(self.signal.shape) * np.nan
        out[self.mask] = pred_sig  
        return out
        
        
    def _calculate_L(self,n):
        """
        Calculate the maximal harmonic order (L), given that you know the
        number of parameters that were estimated. This proceeds according to
        the following logic:

        .. math:: 

           n = \frac{1}{2} (L+1) (L+2)

           \rarrow 2n = L^2 + 3L + 2
           \rarrow L^2 + 3L + 2 - 2n = 0
           \rarrow L^2 + 3L + 2(1-n) = 0

           \rarrow L_{1,2} = \frac{-3 \pm \sqrt{9 - 8 (1-n)}}{2}

           \rarrow L{1,2} = \frac{-3 \pm \sqrt{1 + 8n}}{2}


        Finally, the positive value is chosen between the two options. 
        """

        L1 = (-3 + np.sqrt(1+ 8 *n))/2
        L2 = (-3 - np.sqrt(1+ 8 *n))/2
        
        return max([L1,L2])


class CanonicalTensorModel(BaseModel):
    """
    This is a simplified bi-tensor model, where one tensor is constrained to be a
    sphere and the other is constrained to be a canonical tensor with some
    globally set axial and radial diffusivities (e.g. based on the median ad
    and rd in the 300 highest FA voxels).

    The signal in each voxel can then be described as a linear combination of
    these two factors:

    .. math::
    
       S = \beta_1 S0 e^{-bval \vec{b} q \vec{b}^t} + \beta_2 S0 

    Where $\vec{b}$ is chosen to be one of the diffusion-weighting
    directions used in the scan.

    Consequently, for a particular choice of $\vec{b}$ we can write this as:

    .. math::

       S = X \beta

    Where: S is the nvoxels x ndirections signal from the entire volume, X is a
    2 by ndirections matrix, with one column devoted to the anistropic
    contribution from the canonical tensor and the other column containing a
    constant term in all directions, representing the isotropic
    component. We can solve this equation using OLS fitting and derive the RMSE
    for that choice of $\vec{b}$. For each voxel, we can then find the choice
    of $\vec{b}$ that best predicts the signal (in the least-squares
    sense). This determines the estimated PDD in that voxel.
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 water_diffusivity=3.0,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True):

        """
        Initialize a CanonicalTensorModel class instance.

        Parameters
        ----------

        params_file: str, optional
             full path to the name of the file in which to save the model
             parameters, once a model is fit. 

        over_sample: Provide a finer resolution of directions (in the same
        format that the bvecs come in?), to provide more resolution to the fit
        of the direction of the canonical tensor XXX Still needs to be
        implemented. 

        """
        
        # Initialize the super-class:
        BaseModel.__init__(self,
                            data,
                            bvecs,
                            bvals,
                            affine=affine,
                            mask=mask,
                            scaling_factor=scaling_factor,
                            sub_sample=sub_sample,
                            verbose=verbose)
        
        self.ad = axial_diffusivity
        self.rd = radial_diffusivity
        self.wd = water_diffusivity
        self.params_file = params_file_resolver(self,
                                                'CanonicalTensorModel',
                                                 params_file)

    @desc.auto_attr
    def response_function(self):
        """
        A canonical tensor that describes the presumed response of a single
        fiber 
        """
        return mtt.Tensor(np.diag([self.ad, self.rd, self.rd]),
                              self.bvecs[:,self.b_idx],
                              self.bvals[self.b_idx])

     
    @desc.auto_attr
    def rotations(self):
        """
        These are the canonical tensors pointing in the direction of each of
        the bvecs in the sampling scheme
        """
        # assume S0==1, the fit weight should soak that up:
        return np.array([this.predicted_signal(1) 
                         for this in self.response_function._rotations])

    @desc.auto_attr
    def water_signal(self):
        """
        The signal in a voxel containing only water is predicted by an
        isotropic diffusion tensor, with the mean diffusivity of water at 37 
        C (which is approximately 3.0), pointing to the north pole.
        """
        return mtt.Tensor(
            np.diag([self.wd, self.wd, self.wd]),  # Water!
            self.bvecs[:, self.b_idx],
            self.bvals[:, self.b_idx]).predicted_signal(1)
    
    @desc.auto_attr
    def ols(self):
        """
        Compute the OLS solution. 
        """
        # Preallocate:
        ols_weights = np.empty((len(self.b_idx), 2, self._flat_signal.shape[0]))

        for idx in range(len(self.b_idx)):
            # The 'design matrix':
            d = np.vstack([self.rotations[idx],
                           self.water_signal]).T
            # This is $(X' X)^{-1} X':
            ols_mat = mtu.ols_matrix(d)
            # Multiply to find the OLS solution (fitting to the signal
            # attenuation in each voxel):
            ols_weights[idx] = np.array(np.dot(ols_mat,
                                        self._flat_signal_attenuation.T))
        return ols_weights


    @desc.auto_attr
    def model_params(self):
        """
        The model parameters.

        Similar to the TensorModel, if a fit has ocurred, the data is cached on
        disk as a nifti file 

        If a fit hasn't occured yet, calling this will trigger a model fit and
        derive the parameters.

        In that case, the steps are as follows:

        1. Perform OLS fitting on all voxels in the mask, with each of the
           $\vec{b}$. Choose only the non-negative weights. 

        2. Find the PDD that most readily explains the data (highest
           correlation coefficient between the data and the predicted signal)
           and use that one to derive the fit for that voxel

        """
        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)

            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()
        else:
            # Looks like we might need to do some fitting...
            
            # Get the bvec weights and the isotropic weights
            b_w = self.ols[:,0,:].copy().squeeze()
            i_w = self.ols[:,1,:].copy().squeeze()

            # nan out the places where weights are negative: 
            b_w[np.logical_or(b_w<0, i_w<0)] = np.nan
            i_w[np.logical_or(b_w<0, i_w<0)] = np.nan

            params = np.empty((self._flat_signal.shape[0],3))
            # Find the best OLS solution in each voxel:
            for vox in xrange(self._flat_signal.shape[0]):
                # We do this in each voxel (instead of all at once, which is
                # possible...) to not blow up the memory:
                vox_fits = np.empty(self.rotations.shape)
                for rot_i, rot in enumerate(self.rotations):
                    vox_fits[rot_i] = ((b_w[rot_i,vox] * rot +
                                        i_w[rot_i,vox] * self.water_signal) *
                                       self._flat_S0[vox])

                # Find the predicted signal that best matches the original
                # signal attenuation. That will choose the direction for the
                # tensor we use:
                corrs = mtu.seed_corrcoef(self._flat_signal_attenuation[vox],
                                          vox_fits)
                idx = np.where(corrs==np.nanmax(corrs))[0]

                # Sometimes there is no good solution (maybe we need to fit
                # just an isotropic to all of these?):
                if len(idx):
                    # In case more than one fits the bill, just choose the
                    # first one:
                    if len(idx)>1:
                        idx = idx[0]
                    
                    params[vox,:] = np.array([idx,
                                              b_w[idx, vox],
                                              i_w[idx, vox]]).squeeze()
                else:
                    params[vox,:] = np.array([np.nan, np.nan, np.nan])

                if self.verbose: 
                    if np.mod(vox, 1000)==0:
                        print ("Fit %s voxels, %s percent"%(vox,
                                100*vox/float(self._flat_signal.shape[0])))

            # Save the params for future use: 
            out_params = np.nan * np.ones(self.signal.shape[:3] + (3,))
            out_params[self.mask] = np.array(params).squeeze()
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params

    @desc.auto_attr
    def fit(self):
        """
        Predict the signal attenuation from the fit of the CanonicalTensorModel
        """
        if self.verbose:
            print("Predicting signal from CanonicalTensorModel")

        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            if ~np.isnan(flat_params[vox, 1]):
                out_flat[vox]=(
                    flat_params[vox,1] * self.rotations[flat_params[vox,0]]+
                    flat_params[vox,2] * self.water_signal)
            else:
                out_flat[vox] = np.nan
                
        out = np.nan * np.ones(self.signal.shape)
        out[self.mask] = out_flat

        return out

def err_func_CanonicalTensorModelOpt(x, object, signal):
    """
    The error function for the fit:
    """
    theta,phi,tensor_w,iso_w = x
    # Convert to cartesian coordinates:
    x,y,z = geo.sphere2cart(1, theta, phi)
    bvec = [x,y,z]
    evals, evecs = object.response_function.decompose
    rot_tensor = mtt.tensor_from_eigs(
        evecs * mtu.calculate_rotation(bvec, evecs[0]),
               evals, object.bvecs[:,object.b_idx], object.bvals[:,object.b_idx])

    # Relative to an S0=1:
    pred_sig = tensor_w * rot_tensor.predicted_signal(1) + iso_w
    return pred_sig - signal

class CanonicalTensorModelOpt(CanonicalTensorModel):
    """
    This one is supposed to do the same thing as CanonicalTensorModel, except
    here scipy.optimize is used to find the parameters, instead of OLS fitting.
    """

    @desc.auto_attr
    def model_params(self):
        """
        Find the model parameters using least-squares optimization.
        """

        params = np.empty((self._flat_signal.shape[0],4))
        for vox in range(self._flat_signal.shape[0]):
            # Starting conditions
            mean_sig = np.mean(self._flat_signal[vox])
            x0 = 0,0,mean_sig,mean_sig 
            this_params, status = leastsq(err_func_CanonicalTensorModelOpt,
                                          x0,
                                         (self, self._flat_signal[vox]))
            params[vox] = this_params

            if self.verbose: 
                if np.mod(vox, 1000)==0:
                    print ("Fit %s voxels, %s percent"%(vox,
                            100*vox/float(self._flat_signal.shape[0])))

        out_params = np.nan * np.ones(self.signal.shape[:3] + (3,))
        out_params[self.mask] = np.array(params).squeeze()

            
class MultiCanonicalTensorModel(CanonicalTensorModel):
    """
    This model extends CanonicalTensorModel with the addition of another
    canonical tensor. The logic is similar, but the fitting is done for every
    commbination of sphere + n canonical tensors (where n can be set to any
    number > 1, but can't really realistically be estimated for n>2...).
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True,
                 n_canonicals=2):
        """
        Initialize a MultiCanonicalTensorModel class instance.
        """
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      verbose=verbose)
        
        self.n_canonicals = n_canonicals
        self.params_file = params_file_resolver(self,
                                                'MultiCanonicalTensorModel',
                                                params_file)

    @desc.auto_attr
    def rot_idx(self):
        """
        The indices into combinations of rotations of the canonical tensor,
        according to the order we will use them in fitting
        """
        # Use stdlib magic to make the indices into the basis set: 
        pre_idx = itertools.combinations(range(len(self.b_idx)),
                                         self.n_canonicals)

        # Generate all of them and store, so you know where you stand
        rot_idx = []
        for i in pre_idx:
            rot_idx.append(i)

        return rot_idx

    @desc.auto_attr
    def ols(self):
        """
        Compute the design matrices the matrices for OLS fitting and the OLS
        solution. Cache them for reuse in each direction over all voxels.
        """
        ols_weights = np.empty((len(self.rot_idx),
                                self.n_canonicals + 1,
                                self._flat_signal.shape[0]))
        where_are_we = 0
        for row, idx in enumerate(self.rot_idx):                
        # 'row' refers to where we are in ols_weights
            if self.verbose:
                if idx[0]==where_are_we:
                    s = "Starting MultiCanonicalTensorModel fit"
                    s += " for %sth set of basis functions"%(where_are_we) 
                    print (s)
                    where_are_we += 1
            # The 'design matrix':
            d = np.vstack([[self.rotations[i] for i in idx],
                                np.ones(self.b_idx.shape[0])]).T
            # This is $(X' X)^{-1} X':
            ols_mat = mtu.ols_matrix(d)
            # Multiply to find the OLS solution (fit to signal attenuation):
            ols_weights[row] = np.array(
                np.dot(ols_mat, self._flat_signal_attenuation.T)).squeeze()

        return ols_weights

    @desc.auto_attr
    def model_params(self):
        """
        The model parameters.

        Similar to the CanonicalTensorModel, if a fit has ocurred, the data is
        cached on disk as a nifti file 

        If a fit hasn't occured yet, calling this will trigger a model fit and
        derive the parameters.

        In that case, the steps are as follows:

        1. Perform OLS fitting on all voxels in the mask, with each of the
           $\vec{b}$ combinations, choosing only sets for which all weights are
           non-negative. 

        2. Find the PDD combination that most readily explains the data (highest
           correlation coefficient between the data and the predicted signal)
           That will be the combination used to derive the fit for that voxel.

        """
        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)

            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()
        else:
            # Looks like we might need to do some fitting... 

            # Get the bvec weights (we don't know how many...) and the
            # isotropic weights (which are always last): 
            b_w = self.ols[:,:-1,:].copy().squeeze()
            i_w = self.ols[:,-1,:].copy().squeeze()

            # nan out the places where weights are negative: 
            b_w[b_w<0] = np.nan
            i_w[i_w<0] = np.nan

            # Weight for each canonical tensor, plus a place for the index into
            # rot_idx and one more slot for the isotropic weight (at the end)
            params = np.empty((self._flat_signal.shape[0],
                               self. n_canonicals + 2))

            # Find the best OLS solution in each voxel:
            for vox in xrange(self._flat_signal.shape[0]):
                # We do this in each voxel (instead of all at once, which is
                # possible...) to not blow up the memory:
                vox_fits = np.empty((len(self.rot_idx), len(self.b_idx)))
                
                for idx, rot_idx in enumerate(self.rot_idx):
                    vox_fits[idx] = i_w[idx,vox]
                    vox_fits[idx] += (np.dot(b_w[idx,:,vox],
                                np.array([self.rotations[x] for x in rot_idx])))

                # Find the predicted signal that best matches the original
                # signal attenuation. That will choose the direction for the
                # tensor we use:
                corrs = mtu.seed_corrcoef(self._flat_signal_attenuation[vox],
                                          vox_fits)
                
                idx = np.where(corrs==np.nanmax(corrs))[0]

                # Sometimes there is no good solution:
                if len(idx):
                    # In case more than one fits the bill, just choose the
                    # first one:
                    if len(idx)>1:
                        idx = idx[0]
                    
                    params[vox,:] = np.hstack([idx,
                        np.array([x for x in b_w[idx,:,vox]]).squeeze(),
                        i_w[idx, vox]])
                else:
                    # In which case we set it to all nans:
                    params[vox,:] = np.hstack([np.nan,
                                               self.n_canonicals * (np.nan,),
                                               np.nan])

                if self.verbose: 
                    if np.mod(vox, 1000)==0:
                        print ("Fit %s voxels, %s percent"%(vox,
                                100*vox/float(self._flat_signal.shape[0])))

            # Save the params for future use: 
            out_params = np.nan * np.ones(self.signal.shape[:3]+
                                        (params.shape[-1],))
            out_params[self.mask] = np.array(params).squeeze()
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params

    @desc.auto_attr
    def fit(self):
        """
        Predict the signal attenuation from the fit of the
     MultiCanonicalTensorModel 
        """
        if self.verbose:
            print("Predicting signal from MultiCanonicalTensorModel")

        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            # If there's a nan in there, just ignore this voxel and set it to
            # all nans:
            if ~np.any(np.isnan(flat_params[vox, 1])):
                b_w = flat_params[vox,1:1+self.n_canonicals]
                i_w = flat_params[vox,-1]
                # This gets saved as a float, but we can safely assume it's
                # going to be an integer:
                rot_idx = self.rot_idx[int(flat_params[vox,0])]

                out_flat[vox]=(np.dot(b_w,
                               np.array([self.rotations[i] for i in rot_idx])) +
                               i_w)
            else:
                out_flat[vox] = np.nan  # This gets broadcast to the right
                                        # length on assigment?
        
        out = np.nan * np.ones(self.signal.shape)
        out[self.mask] = out_flat

        return out

class TissueFractionModel(CanonicalTensorModel):
    """
    This is an extension of the CanonicalTensorModel, based on Mazer et al.'s
    measurement of the tissue fraction in different parts of the brain. The
    model posits that tissue fraction accounts for non-free water, restriced or
    hindered by tissue components, which can be represented by a canonical
    tensor and a sphere. The rest (1-tf) is free water, which is represented by
    a second sphere (free water).

    Thus, the model is as follows: 

    .. math:

    \begin{pmatrix} D_1 \\ D_2 \\ ... \\D_n \\ TF \end{pmatrix} =

    \begin{pmatrix} T_1 & D_g & D_iso \\ T_2 & D_g & D_iso \\ T_n & D_g & D_iso
    \\ ... & ... & ... \\ \lambda_1 & \lambda_2 & 0 \end{pmatrix}
    \begin{pmatrix} w_1 & w_2 & w_3 \end{pmatrix}

    And w_2, w_3 are the proportions of tissue-hinderd and free water
    respectively. See below for the estimation proceure
    
    Parameters
    ----------

    tissue_fraction: Full path to a file containing the TF, registered to the
    DWI data and resampled to the DWI data resolution.

    """

    def __init__(self,
                 tissue_fraction,
                 data,
                 bvecs,
                 bvals,
                 l1=0.32,
                 l2=0.15,
                 water_D=0.75,
                 gray_D=0.25,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True):
        
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      verbose=verbose)

        self.tissue_fraction = ni.load(tissue_fraction).get_data()
        self.params_file = params_file_resolver(self,
                                                'TissueFractionModel',
                                                params_file)

        # Set the diffusivity constants:
        self.gray_D = gray_D
        self.water_D = water_D

        # We're going to grid-search over these:
        self.l1 = l1
        self.l2 = l2
        
    @desc.auto_attr
    def _flat_tf(self):
        """
        Flatten the TF

        """
        return self.tissue_fraction[self.mask]


    @desc.auto_attr
    def signal(self):
        """
        The relevant signal here is:

        .. math::

           \begin{pmatrix} \frac{S_1}{S^0_1} \\ \frac{S_2}{S^0_2} \\ ... \\
           \frac{S_3}{S^0_3} \\ TF \end{pmatrix} 
        
        """
        dw_signal = self.data[...,self.b_idx].squeeze()
        tf_signal = np.reshape(self.tissue_fraction,
                               self.tissue_fraction.shape + (1,))

        return np.concatenate([dw_signal, tf_signal], -1)

    @desc.auto_attr
    def signal_attenuation(self):
        """
        The signal attenuation in each b-weighted volume, relative to the mean
        of the non b-weighted volumes. We add the original TF here as a last
        volume, so that we can compare fit to signal. 

        Note
        ----
        Need to overload this function for this class, so that the TF does not
        get attenuated.  

        """
        dw_att= self.data[...,self.b_idx]/np.reshape(self.S0,
                                                       (self.S0.shape + (1,)))

        tf_signal = np.reshape(self.tissue_fraction,
                               self.tissue_fraction.shape + (1,))

        return np.concatenate([dw_att, tf_signal], -1) 

    @desc.auto_attr
    def model_params(self):
        """
        Fitting the weights for the TissueFractionModel is done as a second
        stage, after done fitting the CanonicalTensorModel.
        
        The logic is as follows:

        The isotropic weight calculated in the previous stage subsumes two
        different components: one is the free water isotropic component and the
        other is a hindered tissue water component.

        .. math::

            \w_{iso} = \w_2 D_g + \w_3 D_{csf}
            
        Where $\w_{iso}$ is the weight for the isotropic component fit for
        the initial fit and $\w_{2,3}$ are the weights of tissue water and
        free water respectively. $D_g \approx 1$ and $D_{csf} \approx 3$ are
        the diffusivities of gray and white matter, respectively. 

        In addition, we know that the tissue water, together with the tensor
        signal should account for the tissue fraction measurement:

        .. math::
        
            TF = \w_1 * \lambda_1 + \w_2 * \lambda_2 

        Where $\w_1$ is the weight for the canonical tensor found in
        CanonicalTensorModel and $\w_2$ is the weight on the tissue isotropic
        component. $\lambda_{1,2}$ are additional relative weights of the two
        components within the tissue  (canonical tensor and tissue
        water). Implicitly, $\lambda_3 = 0$, reflecting the fact that the free
        water is not part of the tissue fraction at all. To find \lambda{i}, we
        perform a grid search over plausible values of these and choose the
        ones that best account for the diffusion and TF signal.

        To find $\w_2$ and $\w_3$, we follow these steps:

        0. We find $\w_1 = \w_{tensor}$ using the CanonicalTensorModel
        
        1. We fix the values of \lambda_1 and \lambda_2 and solve for \w_2:

            \w_2 = \frac{TF - \lambda_1 \w_1}{\lambda2} =

        2. From the first equation above, we can then solve for \w_3:

            \w_3 = \w_{iso} - \w_2
            
        3. We go back to the expanded model and predict the diffusion and the
        TF data for these values of     

        """

        # Start by getting the params for the underlying CanonicalTensorModel:
        temp_p_file = self.params_file
        self.params_file = params_file_resolver(self,
                                                'CanonicalTensorModel')
        tensor_params = super(TissueFractionModel, self).model_params
        
        # Restore order: 
        self.params_file = temp_p_file

        # The tensor weight is the second parameter in each voxel: 
        w_ten = tensor_params[self.mask, 1]
        # And the isotropic weight is the third:
        w_iso = tensor_params[self.mask, 2]

        w2 = (self._flat_tf - self.l1 * w_ten) / self.l2
        w3 = (1 - w_ten - w2)

        w2_out = np.nan * np.ones(self.shape[:3])
        w3_out = np.nan * np.ones(self.shape[:3])

        w2_out[self.mask] = w2
        w3_out[self.mask] = w3

        # Return tensor_idx, w1, w2, w3 
        return tensor_params[...,0],tensor_params[...,1], w2_out, w3_out

    
    @desc.auto_attr
    def fit(self):
        """
        Derive the fit of the TissueFractionModel
        """
        if self.verbose:
            print("Predicting signal from TissueFractionModel")

        out_flat = np.empty((self._flat_signal.shape[0],
                            self._flat_signal.shape[1] + 1))

        flat_ten_idx = self.model_params[0][self.mask]
        flat_w1 = self.model_params[1][self.mask]
        flat_w2 = self.model_params[2][self.mask]
        flat_w3 = self.model_params[3][self.mask]

        for vox in xrange(out_flat.shape[0]):
            if ~np.any(np.isnan([flat_w1[vox], flat_w2[vox], flat_w3[vox]])):
                ten = (flat_w1[vox] *
                       np.hstack([self.rotations[flat_ten_idx[vox]], self.l1]))
                tissue_water = flat_w2[vox] * np.hstack(
                [self.gray_D * np.ones(self._flat_signal.shape[-1]) , self.l2])
                free_water = flat_w3[vox] * np.hstack(
                [self.water_D * np.ones(self._flat_signal.shape[-1]) , 0])

                # recover the signal attenuation:
                out_flat[vox]=(ten + tissue_water + free_water) 
            else:
                out_flat[vox] = np.nan
                
        out = np.nan * np.ones(self.signal.shape)
        out[self.mask] = out_flat

        return out

    
class FiberModel(BaseModel):
    """
    
    A class for representing and solving predictive models based on
    tractography solutions.
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 FG,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None):
        """
        Parameters
        ----------
        
        FG: a osmosis.fibers.FiberGroup object, or the name of a pdb file
            containing the fibers to be read in using mtf.fg_from_pdb

        axial_diffusivity: The axial diffusivity of a single fiber population.

        radial_diffusivity: The radial diffusivity of a single fiber population.
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                            data,
                            bvecs,
                            bvals,
                            affine=affine,
                            mask=mask,
                            scaling_factor=scaling_factor,
                            sub_sample=sub_sample)

        self.axial_diffusivity = axial_diffusivity
        self.radial_diffusivity = radial_diffusivity

        # This one also has a fiber group, which is xformed to match the
        # coordinates of the DWI: 
        self.FG = FG.xform(self.affine.getI(), inplace=False)

    @desc.auto_attr
    def fg_idx(self):
        """
        Indices into the coordinates of the fiber-group
        """
        return self.fg_coords.astype(int)
    
    @desc.auto_attr
    def fg_coords(self):
        """
        All the coords of all the fibers  
        """
        return self.FG.coords

    @desc.auto_attr
    def fg_idx_unique(self):
        """
        The *unique* voxel indices
        """
        return mtu.unique_rows(self.fg_idx.T).T

    @desc.auto_attr
    def voxel2fiber(self):
        """
        The first list in the tuple answers the question: Given a voxel (from
        the unique indices in this model), which fibers pass through it?

        The second answers the question: Given a voxel, for each fiber, which
        nodes are in that voxel? 
        """
        # Preallocate for speed:
        
        # Make a voxels by fibers grid. If the fiber is in the voxel, the value
        # there will be 1, otherwise 0:
        v2f = np.zeros((len(self.fg_idx_unique.T), len(self.FG.fibers)))

        # This is a grid of size (fibers, maximal length of a fiber), so that
        # we can capture put in the voxel number in each fiber/node combination:
        v2fn = np.nan * np.ones((len(self.FG.fibers),
                         np.max([f.coords.shape[-1] for f in self.FG])))

        # In each fiber:
        for f_idx, f in enumerate(self.FG.fibers):
            # In each voxel present in there:
            for vv in f.coords.astype(int).T:
                # What serial number is this voxel in the unique fiber indices:
                voxel_id = np.where((vv[0] == self.fg_idx_unique[0]) *
                                    (vv[1] == self.fg_idx_unique[1]) *
                                    (vv[2] == self.fg_idx_unique[2]))[0]
                # Add that combination to the grid:
                v2f[voxel_id,f_idx] += 1 
                # All the nodes going through this voxel get its number:
                v2fn[f_idx][np.where((f.coords.astype(int)[0]==vv[0]) *
                                     (f.coords.astype(int)[1]==vv[1]) *
                                     (f.coords.astype(int)[2]==vv[2]))]=voxel_id
            
            if self.verbose and np.mod(f_idx, 100)==0:
                print("V2F: Done with: %s percent"%(100*
                                (float(f_idx)/len(self.FG.fibers))))

        return v2f,v2fn


    @desc.auto_attr
    def fiber_tensors(self):
        """
        The tensors for each fiber along it's length
        """
        ten = np.empty(len(self.FG.fibers), dtype='object')
        # In each fiber:
        for f_idx, f in enumerate(self.FG):
            ten[f_idx] = f.tensors(self.bvecs[:, self.b_idx],
                                 self.bvals[:, self.b_idx],
                                 self.axial_diffusivity,
                                 self.radial_diffusivity)

            if self.verbose and np.mod(f_idx, 100)==0:
                print("fiber_tensors: Done with: %s percent"%(100*
                                (float(f_idx)/len(self.FG.fibers))))

        return ten
        
    @desc.auto_attr
    def matrix(self):
        """
        The matrix of fiber-contributions to the DWI signal.
        """
        # Assign some local variables, for shorthand:
        vox_coords = self.fg_idx_unique.T
        n_vox = self.fg_idx_unique.shape[-1]
        n_bvecs = self.b_idx.shape[0]
        n_fibers = self.FG.n_fibers
        v2f,v2fn = self.voxel2fiber

        if self.verbose:
            print "Done with voxel2fiber"

        # How many fibers in each voxel (this will determine how many
        # components are in the fiber part of the matrix):
        n_unique_f = np.sum(v2f)        
        
        # Preallocate these, which will be used to generate the two sparse
        # matrices:

        # This one will hold the fiber-predicted signal
        f_matrix_sig = np.zeros(n_unique_f * n_bvecs)
        f_matrix_row = np.zeros(n_unique_f * n_bvecs)
        f_matrix_col = np.zeros(n_unique_f * n_bvecs)

        # And this will hold weights to soak up the isotropic component in each
        # voxel: 
        i_matrix_sig = np.zeros(n_vox * n_bvecs)
        i_matrix_row = np.zeros(n_vox * n_bvecs)
        i_matrix_col = np.zeros(n_vox * n_bvecs)
        
        keep_ct1 = 0
        keep_ct2 = 0

        # In each voxel:
        for v_idx, vox in enumerate(vox_coords):
            # For each fiber:
            for f_idx in np.where(v2f[v_idx])[0]:
                # Sum the signal from each node of the fiber in that voxel: 
                pred_sig = np.zeros(n_bvecs)
                for n_idx in np.where(v2fn[f_idx]==v_idx)[0]: 
                    # Predict the signal and demean it, so that the isotropic
                    # part can carry that:
                    pred_sig +=\
                        (self.fiber_tensors[f_idx][n_idx].predicted_signal(
                        self.S0[vox[0],vox[1],vox[2]]) -
                        np.mean(self.signal[vox[0],vox[1],vox[2]]))
                    
            # For each fiber-voxel combination, we now store the row/column
            # indices and the signal in the pre-allocated linear arrays
            f_matrix_row[keep_ct1:keep_ct1+n_bvecs] =\
                np.arange(n_bvecs) + v_idx * n_bvecs
            f_matrix_col[keep_ct1:keep_ct1+n_bvecs] = np.ones(n_bvecs) * f_idx 
            f_matrix_sig[keep_ct1:keep_ct1+n_bvecs] = pred_sig
            keep_ct1 += n_bvecs

            # Put in the isotropic part in the other matrix: 
            i_matrix_row[keep_ct2:keep_ct2+n_bvecs]=\
                np.arange(v_idx*n_bvecs, (v_idx + 1)*n_bvecs)
            i_matrix_col[keep_ct2:keep_ct2+n_bvecs]= v_idx * np.ones(n_bvecs)
            i_matrix_sig[keep_ct2:keep_ct2+n_bvecs] = 1
            keep_ct2 += n_bvecs
            if self.verbose and np.mod(v_idx,100)==0:
                print("Built %s percent"%(100 * float(v_idx)/len(vox_coords)))
        
        # Allocate the sparse matrices, using the more memory-efficient 'csr'
        # format: 
        fiber_matrix = sps.coo_matrix((f_matrix_sig,
                                       [f_matrix_row, f_matrix_col])).tocsr()
        iso_matrix = sps.coo_matrix((i_matrix_sig,
                                       [i_matrix_row, i_matrix_col])).tocsr()

        if self.verbose:
            print("Generated model matrices")

        return (fiber_matrix, iso_matrix)
                
        
    @desc.auto_attr
    def voxel_signal(self):
        """        
        The signal in the voxels corresponding to where the fibers pass through.
        """ 
        return self.signal[self.fg_idx_unique[0],
                           self.fg_idx_unique[1],
                           self.fg_idx_unique[2]].ravel()

    @desc.auto_attr
    def voxel_signal_demeaned(self):
        """        
        The signal in the voxels corresponding to where the fibers pass
        through, with mean removed
        """
        # First get the signal
        a = self.signal[self.fg_idx_unique[0],
                        self.fg_idx_unique[1],
                        self.fg_idx_unique[2]] 

        # Get the average, broadcast it back to the original shape and ravel
        # and finally remove:
        return(a.ravel() -
               (np.mean(a,-1)[np.newaxis,...] +
                np.zeros((len(self.b_idx),a.shape[0]))).T.ravel())
    
    @desc.auto_attr
    def iso_weights(self):
        """
        Get the weights using scipy.sparse.linalg or sklearn.linear_model.sparse

        """
        if self.verbose:
            show=True
        else:
            show=False

        iso_w, istop, itn, r1norm, r2norm, anorm, acond, arnorm, xnorm, var=\
        sla.lsqr(self.matrix[1], self.voxel_signal, show=show,
                 iter_lim=10e10, atol=10e-10, btol=10e-10, conlim=10e10)

        if istop not in [1,2]:
            warnings.warn("LSQR did not properly converge")

        return iso_w
    
    @desc.auto_attr
    def fiber_weights(self):
        """
        Get the weights for the fiber part of the matrix
        """
        fiber_w =  self._Lasso.coef_
        return fiber_w


    @desc.auto_attr
    def _Lasso(self):
        """
        This is the sklearn spLasso object. XXX Maybe needs some more
        param-settin options...   
        """
        return spLasso().fit(self.matrix[0], self.voxel_signal_demeaned)
    
    @desc.auto_attr
    def _fiber_fit(self):
        """
        This is the fit for the non-isotropic part of the signal:
        """
        return self._Lasso.predict(self.matrix[0])


    @desc.auto_attr
    def _iso_fit(self):
        # We want this to have the size of the original signal which is
        # (n_bvecs * n_vox), so we broadcast across directions in each voxel:
        return (self.iso_weights[np.newaxis,...] +
                np.zeros((len(self.b_idx), self.iso_weights.shape[0]))).T.ravel()


    @desc.auto_attr
    def fit(self):
        """
        The predicted signal from the FiberModel
        """
        # We generate the lasso prediction and in each voxel, we add the
        # offset, according to the isotropic part of the signal, which was
        # removed prior to fitting:
        
        return self._fiber_fit + self._iso_fit
               
                
class SparseDeconvolutionModel(CanonicalTensorModel):
    """
    Use the lasso to do spherical deconvolution with a canonical tensor basis
    set. 
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 solver=None,
                 solver_params=None,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True):
        """
        Initialize SparseDeconvolutionModel class instance.
        """
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      verbose=verbose)
        
        # For now, the default is Lasso:
        if solver is None:
            self.solver = 'Lasso'
        else:
            self.solver = solver

        self.params_file = params_file_resolver(self,
                                            'SparseDeconvolutionModel%s'%solver,
                                             params_file)


        # This will be passed as kwarg to the solver initialization:
        self.solver_params = solver_params

    @desc.auto_attr
    def _solver(self):
        """
        Choose the sklearn solver to be used:
        """
        chosen_solver = sklearn_solvers[self.solver]

        if self.solver_params is not None:
            return chosen_solver(self.solver_params) 
        else:
            return chosen_solver()


    @desc.auto_attr
    def model_params(self):
        """
        Use sklearn to fit the parameters:
        """

        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)

            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()

        else:

            # One weight for each rotation
            params = np.empty((self._flat_signal.shape[0],
                               self.rotations.shape[0]))

            # We fit the deviations from the mean signal, which is why we also
            # demean each of the basis functions:
            design_matrix = self.rotations - np.mean(self.rotations,0)

            # One basis function per column (instead of rows):
            design_matrix = design_matrix.T
            
            for vox in xrange(self._flat_signal.shape[0]):
                # Fit the deviations from the mean: 
                sig = self._flat_signal[vox] - np.mean(self._flat_signal[vox])
                try:
                    params[vox] = self._solver.fit(design_matrix, sig).coef_
                except:
                    print "could not fit here: %s"%vox
                    params[vox] = np.nan * np.ones(design_matrix.shape[-1])

                if self.verbose and np.mod(vox,1000)==0:
                    print("Fit %s percent"%(100*float(vox)/
                                            self._flat_signal.shape[0]))


            out_params = np.nan * np.ones((self.signal.shape[:3] + 
                                          (design_matrix.shape[-1],)))

            out_params[self.mask] = params
            # Save the params to a file: 
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params
            

    @desc.auto_attr
    def fit(self):
        """
        Predict the data from the fit of the SparseDeconvolutionModel
        """
        if self.verbose:
            msg = "Predicting signal from SparseDeconvolutionModel"
            msg += "with %s"%self.solver
            print(msg)

        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            # Add back the mean signal:
            out_flat[vox] = (np.dot(flat_params[vox], self.rotations) +
                             np.mean(self._flat_signal[vox]))
            
        out = np.nan * np.ones(self.signal.shape)
        out[self.mask] = out_flat

        return out