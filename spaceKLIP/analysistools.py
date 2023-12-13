from __future__ import division

import matplotlib
matplotlib.rcParams.update({'font.size': 14})


# =============================================================================
# IMPORTS
# =============================================================================

import os
import pdb
import sys

import astropy.io.fits as fits
from astropy.table import Table
import astropy.units as u

import matplotlib.pyplot as plt
from cycler import cycler
import numpy as np

import copy
import pyklip.fakes as fakes
import pyklip.fitpsf as fitpsf
import pyklip.fm as fm
import pyklip.fmlib.fmpsf as fmpsf
import shutil

from pyklip import klip, parallelized
from pyklip.instruments.JWST import JWSTData
from scipy.ndimage import gaussian_filter, rotate
from scipy.ndimage import shift as spline_shift
from scipy.interpolate import interp1d
from spaceKLIP import utils as ut
from spaceKLIP.psf import get_offsetpsf, JWST_PSF
from spaceKLIP.starphot import get_stellar_magnitudes, read_spec_file
from spaceKLIP.pyklippipeline import get_pyklip_filepaths
from spaceKLIP.utils import write_starfile, set_surrounded_pixels

from webbpsf.constants import JWST_CIRCUMSCRIBED_DIAMETER

from tqdm.auto import trange
from io import StringIO
import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# =============================================================================
# MAIN
# =============================================================================

class AnalysisTools():
    """
    The spaceKLIP astrophysical analysis tools class.
    
    """
    
    def __init__(self,
                 database):
        """
        Initialize the spaceKLIP astrophysical analysis tools class.
        
        Parameters
        ----------
        database : spaceKLIP.Database
            SpaceKLIP database on which the astrophysical analysis steps shall
            be run.
        
        Returns
        -------
        None.
        
        """
        
        # Make an internal alias of the spaceKLIP database class.
        self.database = database
        
        pass
    
    def raw_contrast(self,
                     starfile,
                     spectral_type='G2V',
                     companions=None,
                     overwrite_crpix=None,
                     subdir='rawcon',
                     output_filetype='npy'):
        """
        Compute the raw contrast relative to the provided host star flux.
        
        Parameters
        ----------
        starfile : path
            Path of VizieR VOTable containing host star photometry or two
            column TXT file with wavelength (micron) and flux (Jy).
        spectral_type : str, optional
            Host star spectral type for the stellar model SED. The default is
            'G2V'.
        companions : list of list of three float, optional
            List of companions to be masked before computing the raw contrast.
            For each companion, there should be a three element list containing
            [RA offset (arcsec), Dec offset (arcsec), mask radius (lambda/D)].
            The default is None.
        overwrite_crpix : tuple of two float, optional
            Overwrite the PSF center with the (CRPIX1, CRPIX2) values provided
            here (in 1-indexed coordinates). This is required for Coron3 data!
            The default is None.
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'rawcon'.
        output_filetype : str
            File type to save the raw contrast information to. Options are 'ecsv'
            or 'npy'.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        if companions is not None:
            if not isinstance(companions[0], list):
                if len(companions) == 3:
                    companions = [companions]
            for i in range(len(companions)):
                if len(companions[i]) != 3:
                    raise UserWarning('There should be three elements for each companion in the companions list')
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Copy the starfile that will be used into this directory
        starfile_ext = os.path.splitext(starfile)[1]
        new_starfile_path = output_dir+'/user_starfile'+starfile_ext
        new_header = '#'+starfile.split('/')[-1] + ' /// {}'.format(spectral_type)+'\n'
        write_starfile(starfile, new_starfile_path, new_header)

        # Loop through concatenations.
        for i, key in enumerate(self.database.red.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.red[key])
            for j in range(nfitsfiles):
                
                # Get stellar magnitudes and filter zero points.
                mstar, fzero = get_stellar_magnitudes(starfile, spectral_type, self.database.red[key]['INSTRUME'][j], output_dir=output_dir)  # vegamag, Jy
                
                tp_comsubst = ut.get_tp_comsubst(self.database.red[key]['INSTRUME'][j],
                                                 self.database.red[key]['SUBARRAY'][j],
                                                 self.database.red[key]['FILTER'][j])
                
                # Read FITS file and PSF mask.
                fitsfile = self.database.red[key]['FITSFILE'][j]
                data, head_pri, head_sci, is2d = ut.read_red(fitsfile)
                maskfile = self.database.red[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)
                
                # Compute the pixel area in steradian.
                pxsc_arcsec = self.database.red[key]['PIXSCALE'][j] # arcsec
                pxsc_rad = pxsc_arcsec / 3600. / 180. * np.pi  # rad
                pxar = pxsc_rad**2  # sr
                
                # Convert the host star brightness from vegamag to MJy. Use an
                # unocculted model PSF whose integrated flux is normalized to
                # one in order to obtain the theoretical peak count of the
                # star.
                filt = self.database.red[key]['FILTER'][j]
                offsetpsf = get_offsetpsf(self.database.obs[key])
                fstar = fzero[filt] / 10.**(mstar[filt] / 2.5) / 1e6 * np.max(offsetpsf)  # MJy
                
                # Set the inner and outer working angle and compute the
                # resolution element. Account for possible blurring.
                iwa = 1  # pix
                owa = data.shape[1] // 2  # pix
                if self.database.red[key]['TELESCOP'][j] == 'JWST':
                    if self.database.red[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                        diam = 5.2
                    else:
                        diam = JWST_CIRCUMSCRIBED_DIAMETER
                else:
                    raise UserWarning('Data originates from unknown telescope')
                resolution = 1e-6 * self.database.red[key]['CWAVEL'][j] / diam / pxsc_rad  # pix
                if not np.isnan(self.database.obs[key]['BLURFWHM'][j]):
                    resolution = np.hypot(resolution, self.database.obs[key]['BLURFWHM'][j])
                
                # Get the star position.
                if overwrite_crpix is None:
                    center = (head_pri['CRPIX1'] - 1., head_pri['CRPIX2'] - 1.)  # pix (0-indexed)
                else:
                    center = (overwrite_crpix[0] - 1., overwrite_crpix[1] - 1.)  # pix (0-indexed)
                
                # Mask coronagraph spiders, 4QPM edges, etc. 
                if self.database.red[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                    if 'WB' in self.database.red[key]['CORONMSK'][j]:
                        xr = np.arange(data.shape[-1]) - center[0]
                        yr = np.arange(data.shape[-2]) - center[1]
                        xx, yy = np.meshgrid(xr, yr)
                        pa = -np.rad2deg(np.arctan2(xx, yy))
                        pa[pa < 0.] += 360.
                        ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
                        for ww in ww_sci:
                            roll_ref = self.database.obs[key]['ROLL_REF'][ww]  # deg
                            pa1 = (90. - 15. + roll_ref) % 360.
                            pa2 = (90. + 15. + roll_ref) % 360.
                            if pa1 > pa2:
                                temp = (pa > pa1) | (pa < pa2)
                            else:
                                temp = (pa > pa1) & (pa < pa2)
                            data[:, temp] = np.nan
                            pa1 = (270. - 15. + roll_ref) % 360.
                            pa2 = (270. + 15. + roll_ref) % 360.
                            if pa1 > pa2:
                                temp = (pa > pa1) | (pa < pa2)
                            else:
                                temp = (pa > pa1) & (pa < pa2)
                            data[:, temp] = np.nan
                elif self.database.red[key]['EXP_TYPE'][j] in ['MIR_4QPM']:
                    # This is MIRI 4QPM data, want to mask edges. However, close
                    # to the center you don't have a choice. So, want to use 
                    # rectangles with a gap in the center. 

                    # Create array and pad slightly
                    nanmask = np.zeros_like(data[0])
                    pad = 5
                    nanmask = np.pad(nanmask, pad)

                    # Upsample array to improve centering. 
                    samp = 1 #Upsampling factor
                    nanmask = nanmask.repeat(samp, axis=0).repeat(samp, axis=1)

                    # Define rectangle edges
                    rect_width = 10*samp #pixels
                    thinrect_width = 2*samp #pixels

                    cent_rect = [(center[0]+pad)*samp,(center[0]+pad)*samp,
                                 (center[1]+pad)*samp,(center[1]+pad)*samp]
                    rect = [int(cent_rect[i]-(rect_width/2*(-1)**(i%2))) for i in range(4)]
                    thinrect = [int(cent_rect[i]-(thinrect_width/2*(-1)**(i%2))) for i in range(4)]
                    
                    # Define circle mask for center
                    circ_rad = 15*samp #pixels
                    yarr, xarr = np.ogrid[:nanmask.shape[0], :nanmask.shape[1]]
                    rad_dist = np.sqrt((xarr-(center[0]+pad)*samp)**2 + (yarr-(center[1]+pad)*samp)**2)
                    circ = rad_dist < circ_rad

                    # Loop over images
                    ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
                    for ww in ww_sci:
                        # Apply cross
                        roll_ref = self.database.obs[key]['ROLL_REF'][ww]  # deg
                        temp = np.zeros_like(nanmask)
                        temp[:,rect[0]:rect[1]]=1 #Vertical
                        temp[rect[2]:rect[3],:]=1 #Horizontal

                        # Now ensure center isn't completely masked
                        temp[circ] = 0

                        # Apply thin cross
                        temp[:,thinrect[0]:thinrect[1]]=1 #Vertical
                        temp[thinrect[2]:thinrect[3],:]=1 #Horizontal

                        # Rotate the array, include fixed rotation of FQPM edges
                        temp = rotate(temp, 90-roll_ref+4.83544897, reshape=False) 
                        nanmask += temp

                    # If pixel value too high, should be masked, else set to 1. 
                    nanmask[nanmask>=0.5] = np.nan
                    nanmask[nanmask<0.5] = 1

                    # Downsample, remove padding, and mask data
                    nanmask = nanmask[::samp,::samp]
                    nanmask = nanmask[pad:-pad,pad:-pad]
                    nanmask = set_surrounded_pixels(nanmask)
                    data *= nanmask
                elif self.database.red[key]['EXP_TYPE'][j] in ['MIR_LYOT']:
                    raise NotImplementedError()
                
                # Mask companions.
                if companions is not None:
                    for k in range(len(companions)):
                        ra, dec, rad = companions[k]  # arcsec, arcsec, lambda/D
                        yy, xx = np.indices(data.shape[1:])  # pix
                        rr = np.sqrt((xx - center[0] + ra / pxsc_arcsec)**2 + (yy - center[1] - dec / pxsc_arcsec)**2)  # pix
                        rad *= resolution  # pix
                        data[:, rr <= rad] = np.nan
                
                # Compute raw contrast.
                seps = []
                cons = []
                for k in range(data.shape[0]):
                    sep, con = klip.meas_contrast(dat=data[k] * pxar / fstar, iwa=iwa, owa=owa, resolution=resolution, center=center, low_pass_filter=False)
                    seps += [sep * self.database.red[key]['PIXSCALE'][j]]   # arcsec
                    cons += [con]
                seps = np.array(seps)
                cons = np.array(cons)
                
                # If available, apply the coronagraphic transmission before
                # computing the raw contrast.
                if mask is not None:
                    cons_mask = []
                    for k in range(data.shape[0]):
                        _, con_mask = klip.meas_contrast(dat=np.true_divide(data[k], mask) * pxar / fstar, iwa=iwa, owa=owa, resolution=resolution, center=center, low_pass_filter=False)
                        cons_mask += [con_mask]
                    cons_mask = np.array(cons_mask)
                
                # Plot masked data.
                klmodes = self.database.red[key]['KLMODES'][j].split(',')
                fitsfile = os.path.join(output_dir, os.path.split(fitsfile)[1])
                f = plt.figure(figsize=(6.4, 4.8))
                ax = plt.gca()
                xx = np.arange(data.shape[2]) - center[0]  # pix
                yy = np.arange(data.shape[1]) - center[1]  # pix
                extent = (-(xx[0] - 0.5) * pxsc_arcsec, -(xx[-1] + 0.5) * pxsc_arcsec, (yy[0] - 0.5) * pxsc_arcsec, (yy[-1] + 0.5) * pxsc_arcsec)
                ax.imshow(data[-1], origin='lower', cmap='inferno', extent=extent)
                ax.set_xlabel(r'$\Delta$RA [arcsec]')
                ax.set_ylabel(r'$\Delta$Dec [arcsec]')
                ax.set_title('Masked data (' + klmodes[-1] + ' KL)')
                plt.tight_layout()
                plt.savefig(fitsfile[:-5] + '_masked.pdf')
                # plt.show()
                plt.close()
                
                # Plot raw contrast.
                klmodes = self.database.red[key]['KLMODES'][j].split(',')
                fitsfile = os.path.join(output_dir, os.path.split(fitsfile)[1])
                colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
                mod = len(colors)
                f = plt.figure(figsize=(6.4, 4.8))
                ax = plt.gca()
                for k in range(data.shape[0]):
                    if mask is None:
                        ax.plot(seps[k], cons[k], color=colors[k % mod], label=klmodes[k] + ' KL')
                    else:
                        ax.plot(seps[k], cons[k], color=colors[k % mod], alpha=0.3)
                        ax.plot(seps[k], cons_mask[k], color=colors[k % mod], label=klmodes[k] + ' KL')
                ax.set_yscale('log')
                ax.set_ylim([None,1])
                ax.set_xlabel('Separation [arcsec]')
                ax.set_ylabel(r'5-$\sigma$ contrast')
                ax.legend(loc='upper right', ncols=3)
                if mask is None:
                    ax.set_title('Raw contrast')
                else:
                    ax.set_title('Raw contrast (transparent lines excl. mask TP)')
                plt.tight_layout()
                plt.savefig(fitsfile[:-5] + '_rawcon.pdf')
                # plt.show()
                plt.close()

                if output_filetype.lower()=='ecsv':
                    # Save outputs as astropy ECSV text tables
                    columns = [seps[0]]
                    names = ['separation']
                    for i, klmode in enumerate(klmodes):
                        columns.append(cons[i])
                        names.append(f'contrast, N_kl={klmode}')
                        columns.append(cons_mask[i])
                        names.append(f'contrast+mask, N_kl={klmode}')
                    results_table = Table(columns,
                                          names=names)
                    results_table['separation'].unit = u.arcsec
                    # the following needs debugging:
                    #for kw in ['TELESCOP', 'INSTRUME', 'SUBARRAY', 'FILTER', 'CORONMSK', 'EXP_TYPE', 'FITSFILE']:
                    #    results_table.meta[kw] = self.database.red[key][kw][j]

                    output_fn =  fitsfile[:-5]+"_contrast.ecsv"
                    results_table.write(output_fn, overwrite=True)
                    print(f"Contrast results and plots saved to {output_fn}")
                elif output_filetype.lower()=='npy':
                    # Save outputs as numpy .npy files
                    np.save(fitsfile[:-5] + '_seps.npy', seps)
                    np.save(fitsfile[:-5] + '_cons.npy', cons)
                    if mask is not None:
                        np.save(fitsfile[:-5] + '_cons_mask.npy', cons_mask)
                else:
                    raise ValueError('File save format not supported, options are "npy" or "ecsv".')

        pass

    def calibrate_contrast(self,
                           subdir='calcon',
                           rawcon_subdir='rawcon',
                           rawcon_filetype='npy',
                           companions=None,
                           injection_seps='default',
                           injection_pas='default',
                           injection_flux_sigma=20,
                           multi_injection_spacing=None,
                           use_saved=False,
                           thrput_fit_method='median'
                           ):
        """ 
        Compute a calibrated contrast curve relative to the host star flux. 
       
        Parameters
        ----------
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'calcon'.
        rawcon_subdir : str, optional
            Name of the directory where the raw contrast data products have been 
            saved. The default is 'rawcon'.
        rawcon_filetype : str
            Save filetype of the raw contrast files. 
        companions : list of list of three float, optional
            List of companions to be masked before computing the raw contrast.
            For each companion, there should be a three element list containing
            [RA offset (arcsec), Dec offset (arcsec), mask radius (lambda/D)].
            The default is None.
        injection_seps : 1D-array, optional
            List of separations to inject companions at (arcsec). 
        injection_pas : 1D-array, optional
            List of position angles to inject companions at (degrees).  
        injection_flux_sigma : float, optional
            The peak flux of all injected companions in units of sigma, relative 
            to the 1sigma contrast at the injected separation. 
        multi_injection_spacing : int, None, optional
            Spacing between companions injected in a single image. If companions
            are too close then it can pollute the recovered flux. Set to 'None'
            to inject only one companion at a time (lambda/D).
        use_saved : bool, optional
            Toggle to use existing saved injected and recovered fluxes instead of
            repeating the process. 
        thrput_fit_method : str, optional
            Method to use when fitting/interpolating the measure KLIP throughputs 
            across all of the injection positions. 'median' for a median of PAs at
            with the same separation. 'log_grow' for a logistic growth function.
            
        Returns
        -------
        None.
        """

        # Check input.
        if companions is not None:
            if not isinstance(companions[0], list):
                if len(companions) == 3:
                    companions = [companions]
            for i in range(len(companions)):
                if len(companions[i]) != 3:
                    raise UserWarning('There should be three elements for each companion in the companions list')

        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # Get raw contrast directory 
        rawcon_dir = os.path.join(self.database.output_dir, rawcon_subdir)
        if not os.path.exists(rawcon_dir):
            raise TypeError('Raw contrast must be calculated first. "rawcon" subdirectory not found.')

        # Loop through concatenations.
        for i, key in enumerate(self.database.red.keys()):
            log.info('--> Concatenation ' + key)

            # Need to generate the offset PSF we'll be injecting. Best to do
            # this per concatenation to save time.
            if use_saved == True:
                # Don't need to bother generating the PSF, use dummy value
                offsetpsf = 1
            else:
                offsetpsf = get_offsetpsf(self.database.obs[key])

            # Loop through FITS files.
            nfitsfiles = len(self.database.red[key])
            for j in range(nfitsfiles):

                # Read FITS file and PSF mask.
                fitsfile = self.database.red[key]['FITSFILE'][j]
                data, head_pri, head_sci, is2d = ut.read_red(fitsfile)
                maskfile = self.database.red[key]['MASKFILE'][j]
                mask = ut.read_msk(maskfile)

                # Get the raw contrast information with and without mask correction
                file_str = fitsfile.split('/')[-1]
                if rawcon_filetype == 'npy':
                    seps_file = file_str.replace('.fits', '_seps.npy') #Arcseconds
                    rawcons_file = file_str.replace('.fits', '_cons.npy')
                    maskcons_file = file_str.replace('.fits', '_cons_mask.npy')

                    rawseps = np.load(os.path.join(rawcon_dir,seps_file))
                    rawcons = np.load(os.path.join(rawcon_dir,rawcons_file))
                    maskcons = np.load(os.path.join(rawcon_dir,maskcons_file))
                elif rawcon_filetype == 'ecsv':
                    raise NotImplementedError('.ecsv save format not currently supported for \
                        calibrated contrasts. Please use .npy raw contrasts as input.')
                    # contrast_file = file_str.replace('.fits', '_contrast.ecsv')
                    # contrast_path =  os.path.join(rawcon_dir, contrast_file)

                    # rawcon_data = Table.read(contrast_path, format='ascii.ecsv')

                # Read Stage 2 files and make pyKLIP dataset
                filepaths, psflib_filepaths = get_pyklip_filepaths(self.database, key)
                pyklip_dataset = JWSTData(filepaths, psflib_filepaths)

                # Compute the resolution element. Account for possible blurring.
                pxsc_arcsec = self.database.red[key]['PIXSCALE'][j] # arcsec
                pxsc_rad = pxsc_arcsec / 3600. / 180. * np.pi  # rad
                if self.database.red[key]['TELESCOP'][j] == 'JWST':
                    if self.database.red[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                        diam = 5.2
                    else:
                        diam = JWST_CIRCUMSCRIBED_DIAMETER
                else:
                    raise UserWarning('Data originates from unknown telescope')
                resolution = 1e-6 * self.database.red[key]['CWAVEL'][j] / diam / pxsc_rad  # pix
                if not np.isnan(self.database.obs[key]['BLURFWHM'][j]):
                    resolution *= self.database.obs[key]['BLURFWHM'][j]
                resolution_fwhm = 1.025*resolution

                # Get stellar magnitudes and filter zero points, but use the same file as rawcon
                starfile = os.path.join(rawcon_dir, 'user_starfile.txt')
                with open(starfile) as sf:
                    spectral_type = sf.readline().strip('\n').split(' /// ')[-1]
                mstar, fzero = get_stellar_magnitudes(starfile, 
                                                      spectral_type, 
                                                      self.database.red[key]['INSTRUME'][j], 
                                                      output_dir=output_dir)  # vegamag, Jy
                filt = self.database.red[key]['FILTER'][j]
                fstar = fzero[filt] / 10.**(mstar[filt] / 2.5) / 1e6 * np.max(offsetpsf)  # MJy
                fstar *= ((180./np.pi)*3600.)**2/pxsc_arcsec**2 # MJy/sr

                ### Now want to perform the injection and recovery of companions. 
                # Define the seps and PAs to inject companions at
                if injection_seps == 'default':
                    inj_seps = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 
                                1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
                else:
                    inj_seps = injection_seps
                inj_seps_pix = inj_seps / pxsc_arcsec #Convert separation to pixels

                if injection_pas == 'default':
                    if '4QPM' in self.database.red[key]['CORONMSK'][j]:
                        inj_pas = [57.5,147.5,237.5,327.5]
                    elif 'WB' in self.database.red[key]['CORONMSK'][j]:
                        inj_pas = [45.,135.,225.,315.]
                    else:
                        inj_pas = [0,60,120,180,240,300]
                else:
                    inj_pas = injection_pas

                # Determine the fluxes we want to inject the companions at. 
                # Base it on the contrast for the desired separations. Use the
                # contrast with the ~median KL modes.
                median_KL_index = int(len(rawseps)/2)
                cons_cleaned = np.nan_to_num(rawcons[median_KL_index], nan=1)
                contrast_interp = interp1d(rawseps[median_KL_index], 
                                           cons_cleaned,
                                           kind='linear',
                                           bounds_error=False,
                                           fill_value=(1, cons_cleaned[-1]))
                inj_cons = contrast_interp(inj_seps)
                inj_fluxes = inj_cons*fstar #MJy/sr
                inj_fluxes *= injection_flux_sigma/5 # Scale to an N sigma peak flux

                # Going to redefine companion locations in terms of pixels
                companions_pix = []
                if companions is not None:
                    for k in range(len(companions)):
                        ra, dec, rad = companions[k]  # arcsec, arcsec, lambda/D
                        ra_pix = ra / pxsc_arcsec
                        dec_pix = dec / pxsc_arcsec
                        rad_pix = rad * resolution  # pix
                        companions_pix.append([ra_pix, dec_pix, rad_pix])
                else:
                    companions_pix = None

                # Redefine the multi_injection_spacing in terms of pixels
                if multi_injection_spacing is not None:
                    injection_spacing_pix = multi_injection_spacing*resolution
                else:
                    injection_spacing_pix = multi_injection_spacing

                # Need to get exactly the same KLIP arguments that were used for this subtraction.
                klip_args = {}
                klip_args['mode'] = self.database.red[key]['MODE'][j]
                klip_args['annuli'] = self.database.red[key]['ANNULI'][j]
                klip_args['subsections'] = self.database.red[key]['SUBSECTS'][j]
                klip_args['numbasis'] = [int(nb) for nb in self.database.red[key]['KLMODES'][j].split(',')]
                klip_args['algo'] = 'klip' #Currently not logged, may need changing in future. 
                klip_args['maxnumbasis'] = np.max(klip_args['numbasis'])
                inj_subdir = klip_args['mode'] + '_NANNU' + str(klip_args['annuli']) \
                            + '_NSUBS' + str(klip_args['subsections']) + '_' + key +'/'
                klip_args['movement'] = 1 #Currently not logged, fix later. 
                klip_args['calibrate_flux'] = False
                klip_args['highpass'] = False
                klip_args['verbose'] = False
                inj_output_dir = os.path.join(output_dir, inj_subdir)
                if not os.path.exists(inj_output_dir):
                    os.makedirs(inj_output_dir)
                klip_args['outputdir'] = inj_output_dir

                save_string = output_dir+'/'+file_str[:-5]
                if use_saved:
                    log.info('Retrieving saved companion injection and recovery results.')
                    all_inj_seps = np.load(save_string + '_injrec_seps.npy')
                    all_inj_pas = np.load(save_string+'_injrec_pas.npy')
                    all_inj_fluxes = np.load(save_string+'_injrec_inj_fluxes.npy')
                    all_retr_fluxes = np.load(save_string+'_injrec_retr_fluxes.npy')
                else:
                    # Run the injection and recovery process
                    log.info('Injecting and recovering synthetic companions. This may take a while...')
                    inj_rec = inject_and_recover(pyklip_dataset, 
                                                 injection_psf=offsetpsf,
                                                 injection_seps=inj_seps_pix,
                                                 injection_pas=inj_pas,
                                                 injection_spacing=injection_spacing_pix,
                                                 injection_fluxes=inj_fluxes, 
                                                 klip_args=klip_args,
                                                 retrieve_fwhm=resolution_fwhm,
                                                 true_companions=companions_pix)

                    # Unpack everything from the injection and recovery
                    all_inj_seps, all_inj_pas, all_inj_fluxes, all_retr_fluxes = inj_rec

                    # Save these arrays
                    np.save(save_string+'_injrec_seps.npy', all_inj_seps)
                    np.save(save_string+'_injrec_pas.npy', all_inj_pas)
                    np.save(save_string+'_injrec_inj_fluxes.npy', all_inj_fluxes)
                    np.save(save_string+'_injrec_retr_fluxes.npy', all_retr_fluxes)

                # Need to add a point at a separation of zero pixels, assume
                # basically no flux retrieved at zero separation. 
                all_inj_seps = np.append([0],all_inj_seps)
                all_inj_pas = np.append([0],all_inj_pas)
                all_inj_fluxes = np.append([1],all_inj_fluxes)
                zero_sep_retr_flux = 1e-10*np.ones_like(all_retr_fluxes[0])
                all_retr_fluxes = np.vstack([zero_sep_retr_flux,all_retr_fluxes])

                # Separation returned in pixels but we want arcseconds
                all_inj_seps *= pxsc_arcsec

                # Need to loop over each KL mode used to compute a correction
                # for each.
                rawcons_corr = []
                maskcons_corr = []
                all_corrections = []
                for k in range(len(rawseps)): 
                    # Get the raw separation and contrast for this KL mode
                    this_KL_rawseps = rawseps[k]
                    this_KL_rawcons = rawcons[k]
                    this_KL_maskcons = maskcons[k]

                    # Get fluxes for this KL mode subtracted image
                    this_KL_retr_fluxes = all_retr_fluxes[:,k]

                    # Make a table to make things easier
                    results = Table([all_inj_seps, all_inj_pas, all_inj_fluxes, this_KL_retr_fluxes], 
                                    names=('inj_seps', 'inj_pas', 'inj_fluxes', 'retr_fluxes'))

                    # Determine throughput of klip process on the injected flux
                    results['klip_thrputs'] = np.divide(results['retr_fluxes'], results['inj_fluxes'])

                    # Calculate the median across all position angles
                    med_results = results.group_by('inj_seps').groups.aggregate(np.nanmedian)

                    # Need to interpolate or model to determine throughput at actual 
                    # separations of the contrast curve.
                    if thrput_fit_method == 'median':
                        med_interp = interp1d(med_results['inj_seps'], 
                                              med_results['klip_thrputs'],
                                              fill_value=(1e-10, med_results['klip_thrputs'][-1]), 
                                              bounds_error=False,
                                              kind='slinear')
                        contrast_correction = med_interp(this_KL_rawseps)
                    elif thrput_fit_method == 'log_grow':
                        raise NotImplementedError()
                    else:
                        raise ValueError("Invalid thrput_fit_method: " +\
                                "{}, options are 'median' or 'log_grow'".format(thrput_fit_method))

                    # Apply contrast correction
                    rawcons_corr.append(rawcons[k] / contrast_correction)
                    maskcons_corr.append(maskcons[k] / contrast_correction)
                    all_corrections.append(contrast_correction)

                all_corrections = np.squeeze(all_corrections) #Tidy array
                # Need to make sure its not 1D for number of different KL modes == 1 case
                if all_corrections.ndim == 1:
                    all_corrections = all_corrections[np.newaxis, :]

                # Save the corrected contrasts, as well as the separations for convenience. 
                np.save(save_string+'_cal_seps.npy', rawseps)
                np.save(save_string+'_cal_cons.npy', rawcons_corr)
                np.save(save_string+'_cal_maskcons.npy', rawcons_corr)

                # Plot measured KLIP throughputs
                fig = plt.figure(figsize=(6.4, 4.8))
                ax = plt.gca()
                color = plt.cm.tab10(np.linspace(0, 1, 10))
                cc = (cycler(linestyle=['-', ':', '--'])*cycler(color=color))
                ax.set_prop_cycle(cc)
                for ci, corr in enumerate(all_corrections):
                    KLmodes = klip_args['numbasis'][ci]
                    ax.plot(rawseps[ci], corr, label='KL = {}'.format(KLmodes))
                ax.legend(ncol=3, fontsize=10)
                ax.set_xlabel('Separation (")')
                ax.set_ylabel('Throughput')
                ax.grid(axis='both', alpha=0.15)
                plt.savefig(save_string + '_allKL_throughput.pdf', 
                            bbox_inches='tight', dpi=300)

                # Plot individual measurements for median KL mode
                fig = plt.figure(figsize=(6.4, 4.8))
                ax = plt.gca()

                ax.plot(rawseps[median_KL_index], 
                        all_corrections[median_KL_index],
                        label='Applied Correction',
                        color='#0B5345', zorder=100)
                ax.scatter(all_inj_seps, 
                           all_retr_fluxes[:,median_KL_index]/all_inj_fluxes, 
                           s=75, 
                           color='mediumaquamarine', 
                           alpha=0.5,
                           label='Individual Injections')
                ax.legend(fontsize=10)
                ax.set_xlabel('Separation (")')
                ax.set_ylabel('Throughput')
                ax.grid(axis='both', alpha=0.15)
                plt.savefig(save_string + '_medKL_throughput.pdf', 
                            bbox_inches='tight', dpi=300)

                # Plot calibrated contrast curves
                fig = plt.figure(figsize=(6.4, 4.8))
                ax = plt.gca()
                ax.set_prop_cycle(cc)
                for si, seps in enumerate(rawseps):
                    KLmodes = klip_args['numbasis'][si]
                    ax.plot(seps, maskcons_corr[si], 
                            label='KL = {}'.format(KLmodes))
                    ax.plot(seps, rawcons_corr[si], alpha=0.3)
                ax.set_yscale('log')
                ax.set_ylim([None,1])
                ax.set_xlabel('Separation [arcsec]')
                ax.set_ylabel(r'5-$\sigma$ contrast')
                ax.legend(loc='upper right', ncols=3, fontsize=10)
                ax.set_title('Calibrated contrast (transparent lines excl. mask TP)',
                             fontsize=11)
                plt.savefig(save_string + '_calcon.pdf', 
                            bbox_inches='tight', dpi=300)
    
    def extract_companions(self,
                           companions,
                           starfile,
                           mstar_err,
                           spectral_type='G2V',
                           klmode='max',
                           date='auto',
                           use_fm_psf=True,
                           highpass=False,
                           fitmethod='mcmc',
                           fitkernel='diag',
                           subtract=True,
                           inject=False,
                           overwrite=True,
                           subdir='companions'):
        """
        Extract the best fit parameters of a number of companions from each
        reduction in the spaceKLIP reductions database.
        
        Parameters
        ----------
        companions : list of list of three float, optional
            List of companions to be extracted. For each companion, there
            should be a three element list containing guesses for [RA offset
            (arcsec), Dec offset (arcsec), contrast].
        starfile : path
            Path of VizieR VOTable containing host star photometry or two
            column TXT file with wavelength (micron) and flux (Jy).
        mstar_err : float or dict of float
            Error on the host star magnitude. If float, will use the same value
            for each filter. If dict of float, the dictionary keys must be the
            JWST filters in use and a different value can be used for each
            filter.
        spectral_type : str, optional
            Host star spectral type for the stellar model SED. The default is
            'G2V'.
        klmode : int or 'max', optional
            KL mode for which the companions shall be extracted. If 'max', then
            the maximum possible KL mode will be used. The default is 'max'.
        date : str, optional
            Observation date in the format 'YYYY-MM-DDTHH:MM:SS.MMM'. Will
            query for the wavefront measurement closest in time to the given
            date. If 'auto', will grab date from the FITS file header. If None,
            then the default WebbPSF OPD is used (RevAA). The default is
            'auto'.
        use_fm_psf : bool, optional
            If True, use a FM PSF generated with pyKLIP, otherwise use a more
            simple integration time-averaged model offset PSF which does not
            incorporate any KLIP throughput losses. The default is True.
        highpass : bool or float, optional
            If float, will apply a high-pass filter to the FM PSF and KLIP
            dataset. The default is False.
        fitmethod : 'mcmc' or 'nested', optional
            Sampling algorithm which shall be used. The default is 'mcmc'.
        fitkernel : str, optional
            Pyklip.fitpsf.FitPSF covariance kernel which shall be used for the
            Gaussian process regression. The default is 'diag'.
        subtract : bool, optional
            If True, subtract each extracted companion from the pyKLIP dataset
            before fitting the next one in the list. The default is True.
        inject : bool, optional
            Instead of fitting for a companion at the guessed location and
            contrast, inject one into the data.
        overwrite : bool, optional
            If True, compute a new FM PSF and overwrite any existing one,
            otherwise try to load an existing one and only compute a new one if
            none exists yet. The default is True.
        subdir : str, optional
            Name of the directory where the data products shall be saved. The
            default is 'companions'.
        
        Returns
        -------
        None.
        
        """
        
        # Check input.
        kwargs_temp = {}
        
        # Set output directory.
        output_dir = os.path.join(self.database.output_dir, subdir)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Loop through concatenations.
        for i, key in enumerate(self.database.red.keys()):
            log.info('--> Concatenation ' + key)
            
            # Loop through FITS files.
            nfitsfiles = len(self.database.red[key])
            for j in range(nfitsfiles):
                
                # Get stellar magnitudes and filter zero points.
                mstar, fzero, fzero_si = get_stellar_magnitudes(starfile, spectral_type, self.database.red[key]['INSTRUME'][j], return_si=True, output_dir=output_dir)  # vegamag, Jy, erg/cm^2/s/A
                
                # Get COM substrate throughput.
                tp_comsubst = ut.get_tp_comsubst(self.database.red[key]['INSTRUME'][j],
                                                 self.database.red[key]['SUBARRAY'][j],
                                                 self.database.red[key]['FILTER'][j])
                
                # Compute the pixel area in steradian.
                pxsc_arcsec = self.database.red[key]['PIXSCALE'][j] # arcsec
                pxsc_rad = pxsc_arcsec / 3600. / 180. * np.pi  # rad
                pxar = pxsc_rad**2  # sr
                
                # Compute the resolution element. Account for possible
                # blurring.
                if self.database.red[key]['TELESCOP'][j] == 'JWST':
                    if self.database.red[key]['EXP_TYPE'][j] in ['NRC_CORON']:
                        diam = 5.2
                    else:
                        diam = JWST_CIRCUMSCRIBED_DIAMETER
                else:
                    raise UserWarning('Data originates from unknown telescope')
                resolution = 1e-6 * self.database.red[key]['CWAVEL'][j] / diam / pxsc_rad  # pix
                if not np.isnan(self.database.obs[key]['BLURFWHM'][j]):
                    resolution = np.hypot(resolution, self.database.obs[key]['BLURFWHM'][j])
                
                # Find science and reference files.
                filepaths, psflib_filepaths, maxnumbasis = get_pyklip_filepaths(database, key, return_maxbasis=True)
                if 'maxnumbasis' not in kwargs_temp.keys() or kwargs_temp['maxnumbasis'] is None:
                    kwargs_temp['maxnumbasis'] = maxnumbasis
                
                # Initialize pyKLIP dataset.
                dataset = JWSTData(filepaths, psflib_filepaths)
                kwargs_temp['dataset'] = dataset
                kwargs_temp['aligned_center'] = dataset._centers[0]
                kwargs_temp['psf_library'] = dataset.psflib
                
                # Get index of desired KL mode.
                klmodes = self.database.red[key]['KLMODES'][j].split(',')
                klmodes = np.array([int(temp) for temp in klmodes])
                if klmode == 'max':
                    klindex = np.argmax(klmodes)
                else:
                    klindex = klmodes.tolist().index(klmode)
                
                # Set output directories.
                output_dir_kl = os.path.join(output_dir, 'KL%.0f' % klmodes[klindex])
                if not os.path.exists(output_dir_kl):
                    os.makedirs(output_dir_kl)
                
                # Initialize a function that can generate model offset PSFs.
                inst = self.database.red[key]['INSTRUME'][j]
                filt = self.database.red[key]['FILTER'][j]
                apername = self.database.red[key]['APERNAME'][j]
                if self.database.red[key]['TELESCOP'][j] == 'JWST':
                    if inst == 'NIRCAM':
                        pass
                        # image_mask = self.database.red[key]['CORONMSK'][j]
                        # image_mask = image_mask[:4] + image_mask[5:]
                    elif inst == 'NIRISS':
                        raise NotImplementedError()
                    elif inst == 'MIRI':
                        pass
                        # image_mask = self.database.red[key]['CORONMSK'][j].replace('4QPM_', 'FQPM')
                    else:
                        raise UserWarning('Data originates from unknown JWST instrument')
                else:
                    raise UserWarning('Data originates from unknown telescope')
                if starfile is not None and starfile.endswith('.txt'):
                    sed = read_spec_file(starfile)
                else:
                    sed = None
                ww_sci = np.where(self.database.obs[key]['TYPE'] == 'SCI')[0]
                if date is not None:
                    if date == 'auto':
                        date = fits.getheader(self.database.obs[key]['FITSFILE'][ww_sci[0]], 0)['DATE-BEG']
                offsetpsf_func = JWST_PSF(apername,
                                          filt,
                                          date=date,
                                          fov_pix=65,
                                          oversample=2,
                                          sp=sed,
                                          use_coeff=False)
                
                # Loop through companions.
                tab = Table(names=('ID',
                                   'RA',
                                   'RA_ERR',
                                   'DEC',
                                   'DEC_ERR',
                                   'FLUX_JY',
                                   'FLUX_JY_ERR',
                                   'FLUX_SI',
                                   'FLUX_SI_ERR',
                                   'FLUX_SI_ALT',
                                   'FLUX_SI_ALT_ERR',
                                   'CON',
                                   'CON_ERR',
                                   'DELMAG',
                                   'DELMAG_ERR',
                                   'APPMAG',
                                   'APPMAG_ERR',
                                   'MSTAR',
                                   'MSTAR_ERR',
                                   'SNR',
                                   'LN(Z/Z0)',
                                   'TP_CORONMSK',
                                   'TP_COMSUBST',
                                   'FITSFILE'),
                            dtype=('int',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'float',
                                   'object'))
                for k in range(len(companions)):
                    output_dir_comp = os.path.join(output_dir_kl, 'C%.0f' % (k + 1))
                    if not os.path.exists(output_dir_comp):
                        os.makedirs(output_dir_comp)
                    output_dir_fm = os.path.join(output_dir_comp, 'KLIP_FM')
                    if not os.path.exists(output_dir_fm):
                        os.makedirs(output_dir_fm)
                    output_dir_pk = os.path.join(output_dir_comp, 'PREKLIP')
                    if not os.path.exists(output_dir_pk):
                        os.makedirs(output_dir_pk)
                    
                    # Offset PSF that is not affected by the coronagraphic
                    # mask, but only the Lyot stop.
                    psf_no_coronmsk = offsetpsf_func.psf_off
                    
                    # Initial guesses for the fit parameters.
                    guess_dx = companions[k][0] / pxsc_arcsec  # pix
                    guess_dy = companions[k][1] / pxsc_arcsec  # pix
                    guess_flux = companions[k][2]  # contrast
                    guess_spec = np.array([1.])
                    guess_sep = np.sqrt(guess_dx**2 + guess_dy**2)  # pix
                    guess_pa = np.rad2deg(np.arctan2(guess_dx, guess_dy))  # deg
                    
                    # The initial guesses are made in RA/Dec space, but the
                    # model PSFs are defined by the offset between the
                    # coronagraphic mask center and the companion. Hence, we
                    # need to generate a separate model PSF for each roll.
                    rot_offsetpsfs = []
                    sci_totinttime = []
                    all_offsetpsfs = []
                    all_offsetpsfs_nohpf = []
                    all_pas = []
                    scale_factor_avg = []
                    for ww in ww_sci:
                        roll_ref = self.database.obs[key]['ROLL_REF'][ww]  # deg
                        
                        # Get shift between star and coronagraphic mask
                        # position. If positive, the coronagraphic mask center
                        # is to the left/bottom of the star position.
                        _, _, _, _, _, _, _, maskoffs = ut.read_obs(self.database.obs[key]['FITSFILE'][ww])
                        
                        # NIRCam.
                        if maskoffs is not None:
                            mask_xoff = -maskoffs[:, 0]  # pix
                            mask_yoff = -maskoffs[:, 1]  # pix
                            
                            # Need to rotate by the roll angle (CCW) and flip
                            # the x-axis so that positive RA is to the left.
                            mask_raoff = -(mask_xoff * np.cos(np.deg2rad(roll_ref)) - mask_yoff * np.sin(np.deg2rad(roll_ref)))  # pix
                            mask_deoff = mask_xoff * np.sin(np.deg2rad(roll_ref)) + mask_yoff * np.cos(np.deg2rad(roll_ref))  # pix
                            
                            # Compute the true offset between the companion and
                            # the coronagraphic mask center.
                            sim_dx = guess_dx - mask_raoff  # pix
                            sim_dy = guess_dy - mask_deoff  # pix
                            sim_sep = np.sqrt(sim_dx**2 + sim_dy**2) * pxsc_arcsec  # arcsec
                            sim_pa = np.rad2deg(np.arctan2(sim_dx, sim_dy))  # deg
                            
                            # Take median of observation. Typically, each
                            # dither position is a separate observation.
                            sim_sep = np.median(sim_sep)
                            sim_pa = np.median(sim_pa)
                        
                        # Otherwise.
                        else:
                            sim_sep = np.sqrt(guess_dx**2 + guess_dy**2) * pxsc_arcsec  # arcsec
                            sim_pa = np.rad2deg(np.arctan2(guess_dx, guess_dy))  # deg

                        # Generate offset PSF for this roll angle. Do not add
                        # the V3Yidl angle as it has already been added to the
                        # roll angle by spaceKLIP. This is only for estimating
                        # the coronagraphic mask throughput!
                        offsetpsf_coronmsk = offsetpsf_func.gen_psf([sim_sep, sim_pa],
                                                                    mode='rth',
                                                                    PA_V3=roll_ref,
                                                                    do_shift=False,
                                                                    quick=True,
                                                                    addV3Yidl=False)
                        
                        # Coronagraphic mask throughput is not incorporated
                        # into the flux calibration of the JWST pipeline so
                        # that the companion flux from the detector pixels will
                        # be underestimated. Therefore, we need to scale the
                        # model offset PSF to account for the coronagraphic
                        # mask throughput (it becomes fainter). Compute scale
                        # factor by comparing a model PSF with and without
                        # coronagraphic mask.
                        scale_factor = np.sum(offsetpsf_coronmsk) / np.sum(psf_no_coronmsk)
                        scale_factor_avg += [scale_factor]
                        
                        # Normalize model offset PSF to a total integrated flux
                        # of 1 at infinity. Generates a new webbpsf model with
                        # PSF normalization set to 'exit_pupil'.
                        offsetpsf = offsetpsf_func.gen_psf([sim_sep, sim_pa],
                                                           mode='rth',
                                                           PA_V3=roll_ref,
                                                           do_shift=False,
                                                           quick=False,
                                                           addV3Yidl=False,
                                                           normalize='exit_pupil')
                        
                        # Normalize model offset PSF by the flux of the star.
                        offsetpsf *= fzero[filt] / 10**(mstar[filt] / 2.5) / 1e6 / pxar  # MJy/sr
                        
                        # Apply scale factor to incorporate the coronagraphic
                        # mask througput.
                        offsetpsf *= scale_factor
                        
                        # Apply scale factor to incorporate the COM substrate
                        # transmission.
                        # offsetpsf *= tp_comsubst
                        
                        # Blur frames with a Gaussian filter.
                        if not np.isnan(self.database.obs[key]['BLURFWHM'][ww]):
                            gauss_sigma = self.database.obs[key]['BLURFWHM'][j] / np.sqrt(8. * np.log(2.))
                            offsetpsf = gaussian_filter(offsetpsf, gauss_sigma)
                        
                        # Apply high-pass filter.
                        offsetpsf_nohpf = copy.deepcopy(offsetpsf)
                        if not isinstance(highpass, bool):
                            highpass = float(highpass)
                            fourier_sigma_size = (offsetpsf.shape[0] / highpass) / (2. * np.sqrt(2. * np.log(2.)))
                            offsetpsf = parallelized.high_pass_filter_imgs(np.array([offsetpsf]), numthreads=None, filtersize=fourier_sigma_size)[0]
                        else:
                            if highpass:
                                raise NotImplementedError()
                        
                        # Save rotated model offset PSFs in case we do not end
                        # up using FM.
                        nints = self.database.obs[key]['NINTS'][ww]
                        effinttm = self.database.obs[key]['EFFINTTM'][ww]
                        rot_offsetpsf = rotate(offsetpsf, -roll_ref, reshape=False, mode='constant', cval=0.)
                        rot_offsetpsfs.extend([rot_offsetpsf])  # do not duplicate
                        sci_totinttime.extend([nints * effinttm])
                        
                        # Save non-rotated model offset PSFs for the FM.
                        all_offsetpsfs.extend([offsetpsf for ni in range(nints)])
                        all_offsetpsfs_nohpf.extend([offsetpsf_nohpf for ni in range(nints)])
                        all_pas.extend([roll_ref for ni in range(nints)])
                    scale_factor_avg = np.sum([scale_factor_avg[l] * sci_totinttime[l] / np.sum(sci_totinttime)
                                               for l in range(len(scale_factor_avg))])
                    
                    # Compute the FM dataset if it does not exist yet, or if
                    # overwrite is True.
                    fmdataset = os.path.join(output_dir_fm, 'FM-' + key + '-fmpsf-KLmodes-all.fits')
                    klipdataset = os.path.join(output_dir_fm, 'FM-' + key + '-klipped-KLmodes-all.fits')
                    if overwrite or (not os.path.exists(fmdataset) or not os.path.exists(klipdataset)):
                        
                        # Initialize the pyKLIP FM class. Use sep/pa relative
                        # to the star and not the coronagraphic mask center.
                        input_wvs = np.unique(dataset.wvs)
                        if len(input_wvs) != 1:
                            raise NotImplementedError('Only implemented for broadband photometry')
                        fm_class = fmpsf.FMPlanetPSF(inputs_shape=dataset.input.shape,
                                                     numbasis=klmodes,
                                                     sep=guess_sep,
                                                     pa=guess_pa,
                                                     dflux=guess_flux,
                                                     input_psfs=np.array(all_offsetpsfs),
                                                     input_wvs=input_wvs,
                                                     spectrallib=[guess_spec],
                                                     spectrallib_units='contrast',
                                                     field_dependent_correction=None,
                                                     input_psfs_pas=all_pas)
                        
                        # Compute the FM dataset.
                        mode = self.database.red[key]['MODE'][j]
                        annuli = 1
                        subsections = 1
                        if not isinstance(highpass, bool):
                            if k == 0:
                                highpass_temp = float(highpass)
                            else:
                                highpass_temp = False
                        else:
                            if highpass:
                                raise NotImplementedError()
                            else:
                                highpass_temp = False
                        fm.klip_dataset(dataset=dataset,
                                        fm_class=fm_class,
                                        mode=mode,
                                        outputdir=output_dir_fm,
                                        fileprefix='FM-' + key,
                                        annuli=annuli,
                                        subsections=subsections,
                                        movement=1,
                                        numbasis=klmodes,
                                        maxnumbasis=maxnumbasis,
                                        calibrate_flux=False,
                                        psf_library=dataset.psflib,
                                        highpass=highpass_temp,
                                        mute_progression=True)
                    
                    # Open the FM dataset.
                    with fits.open(fmdataset) as hdul:
                        fm_frame = hdul[0].data[klindex]
                        fm_centx = hdul[0].header['PSFCENTX']
                        fm_centy = hdul[0].header['PSFCENTY']
                    with fits.open(klipdataset) as hdul:
                        data_frame = hdul[0].data[klindex]
                        data_centx = hdul[0].header['PSFCENTX']
                        data_centy = hdul[0].header['PSFCENTY']
                    
                    # If use_fm_psf is False, then replace the FM PSF in the
                    # fm_frame with an integration time-averaged model offset
                    # PSF.
                    if use_fm_psf == False:
                        av_offsetpsf = np.average(rot_offsetpsfs, weights=sci_totinttime, axis=0)
                        sx = av_offsetpsf.shape[1]
                        sy = av_offsetpsf.shape[0]
                        
                        # Make sure that the model offset PSF has odd shape and
                        # perform the required subpixel shift before inserting
                        # it into the fm_frame.
                        if (sx % 2 != 1) or (sy % 2 != 1):
                            raise UserWarning('Model offset PSF must be of odd shape')
                        xshift = (fm_centx - int(fm_centx)) - (guess_dx - int(guess_dx))
                        yshift = (fm_centy - int(fm_centy)) + (guess_dy - int(guess_dy))
                        stamp = spline_shift(av_offsetpsf, (yshift, xshift), order=3, mode='constant', cval=0.)
                        
                        # Also need to scale the model offset PSF by the
                        # guessed flux.
                        stamp *= guess_flux
                        
                        # Insert the model offset PSF into the fm_frame.
                        fm_frame[:, :] = 0.
                        fm_frame[int(fm_centy) + int(guess_dy) - sy//2:int(fm_centy) + int(guess_dy) + sy//2 + 1, int(fm_centx) - int(guess_dx) - sx//2:int(fm_centx) - int(guess_dx) + sx//2 + 1] = stamp
                    
                    # Fit the FM PSF to the KLIP-subtracted data.
                    if inject == False:
                        fitboxsize = 30  # pix
                        # fitboxsize = 21  # pix
                        dr = 5  # pix
                        exclusion_radius = 3 * resolution  # pix
                        corr_len_guess = 3.  # pix
                        xrange = 2.  # pix
                        yrange = 2.  # pix
                        # xrange = 0.001  # pix
                        # yrange = 0.001  # pix
                        frange = 10.  # mag
                        corr_len_range = 1.  # mag
                        
                        # MCMC.
                        if fitmethod == 'mcmc':
                            # fm_frame *= -1
                            fma = fitpsf.FMAstrometry(guess_sep=guess_sep,
                                                      guess_pa=guess_pa,
                                                      fitboxsize=fitboxsize)
                            fma.generate_fm_stamp(fm_image=fm_frame,
                                                  fm_center=[fm_centx, fm_centy],
                                                  padding=5)
                            fma.generate_data_stamp(data=data_frame,
                                                    data_center=[data_centx, data_centy],
                                                    dr=dr,
                                                    exclusion_radius=exclusion_radius)
                            corr_len_label = r'$l$'
                            fma.set_kernel(fitkernel, [corr_len_guess], [corr_len_label])
                            # fma.set_kernel('diag', [], [])
                            fma.set_bounds(xrange, yrange, frange, [corr_len_range])
                            
                            # Make sure that the noise map is invertible.
                            noise_map_max = np.nanmax(fma.noise_map)
                            fma.noise_map[np.isnan(fma.noise_map)] = noise_map_max
                            fma.noise_map[fma.noise_map == 0.] = noise_map_max
                            
                            # Run the MCMC fit.
                            nwalkers = 50
                            nburn = 100
                            nsteps = 200
                            numthreads = 4
                            chain_output = os.path.join(output_dir_kl, key + '-bka_chain_c%.0f' % (k + 1) + '.pkl')
                            fma.fit_astrometry(nwalkers=nwalkers,
                                               nburn=nburn,
                                               nsteps=nsteps,
                                               numthreads=numthreads,
                                               chain_output=chain_output)
                            
                            # Plot the MCMC fit results.
                            path = os.path.join(output_dir_kl, key + '-corner_c%.0f' % (k + 1) + '.pdf')
                            fma.make_corner_plot()
                            plt.savefig(path)
                            plt.close()
                            path = os.path.join(output_dir_kl, key + '-model_c%.0f' % (k + 1) + '.pdf')
                            fma.best_fit_and_residuals()
                            plt.savefig(path)
                            plt.close()
                            
                            # Write the MCMC fit results into a table.
                            flux_jy = fma.fit_flux.bestfit * guess_flux
                            flux_jy *= fzero[filt] / 10**(mstar[filt] / 2.5)  # Jy
                            flux_jy_err = fma.fit_flux.error * guess_flux
                            flux_jy_err *= fzero[filt] / 10**(mstar[filt] / 2.5)  # Jy
                            flux_si = fma.fit_flux.bestfit * guess_flux
                            flux_si *= fzero_si[filt] / 10**(mstar[filt] / 2.5)  # erg/cm^2/s/A
                            flux_si *= 1e-7 * 1e4 * 1e4  # W/m^2/um
                            flux_si_err = fma.fit_flux.error * guess_flux
                            flux_si_err *= fzero_si[filt] / 10**(mstar[filt] / 2.5)  # erg/cm^2/s/A
                            flux_si_err *= 1e-7 * 1e4 * 1e4  # W/m^2/um
                            flux_si_alt = flux_jy * 1e-26 * 299792458. / (1e-6 * self.database.red[key]['CWAVEL'][j])**2 * 1e-6  # W/m^2/um
                            flux_si_alt_err = flux_jy_err * 1e-26 * 299792458. / (1e-6 * self.database.red[key]['CWAVEL'][j])**2 * 1e-6  # W/m^2/um
                            delmag = -2.5 * np.log10(fma.fit_flux.bestfit * guess_flux)  # mag
                            delmag_err = 2.5 / np.log(10.) * fma.fit_flux.error / fma.fit_flux.bestfit  # mag
                            if isinstance(mstar_err, dict):
                                mstar_err_temp = mstar_err[filt]
                            else:
                                mstar_err_temp = mstar_err
                            appmag = mstar[filt] + delmag  # vegamag
                            appmag_err = np.sqrt(mstar_err_temp**2 + delmag_err**2)
                            fitsfile = os.path.join(output_dir_kl, key + '-fitpsf_c%.0f' % (k + 1) + '.fits')
                            tab.add_row((k + 1,
                                         fma.raw_RA_offset.bestfit * pxsc_arcsec,  # arcsec
                                         fma.raw_RA_offset.error * pxsc_arcsec,  # arcsec
                                         fma.raw_Dec_offset.bestfit * pxsc_arcsec,  # arcsec
                                         fma.raw_Dec_offset.error * pxsc_arcsec,  # arcsec
                                         flux_jy,
                                         flux_jy_err,
                                         flux_si,
                                         flux_si_err,
                                         flux_si_alt,
                                         flux_si_alt_err,
                                         fma.raw_flux.bestfit * guess_flux,
                                         fma.raw_flux.error * guess_flux,
                                         delmag,  # mag
                                         delmag_err,  # mag
                                         appmag,  # mag
                                         appmag_err,  # mag
                                         mstar[filt],  # mag
                                         mstar_err,  # mag
                                         np.nan,
                                         np.nan,
                                         scale_factor_avg,
                                         tp_comsubst,
                                         fitsfile))
                            
                            # Write the FM PSF to a file for future plotting.
                            ut.write_fitpsf_images(fma, fitsfile, tab[-1])
                        
                        # Nested sampling.
                        elif fitmethod == 'nested':
                            output_dir_ns = os.path.join(output_dir_kl, 'temp-multinest/')
                            
                            # Initialize PlanetEvidence module.
                            try:
                                fit = fitpsf.PlanetEvidence(guess_sep, guess_pa, fitboxsize, output_dir_ns)
                            except ModuleNotFoundError:
                                raise ModuleNotFoundError('Pymultinest is not installed, try\n\"conda install -c conda-forge pymultinest\"')
                            log.info('  --> Initialized PlanetEvidence module')
                            
                            # Generate FM and data stamps.
                            fit.generate_fm_stamp(fm_frame, [fm_centx, fm_centy], padding=5)
                            fit.generate_data_stamp(data_frame, [data_centx, data_centy], dr=dr, exclusion_radius=exclusion_radius)
                            log.info('  --> Generated FM and data stamps')
                            
                            # Set fit kernel.
                            corr_len_guess = 3.  # pix
                            corr_len_label = 'l'
                            fit.set_kernel(fitkernel, [corr_len_guess], [corr_len_label])
                            log.info('  --> Set fit kernel to ' + fitkernel)
                            
                            # Set fit bounds.
                            fit.set_bounds(xrange, yrange, frange, [corr_len_range])
                            log.info('  --> Set fit bounds')
                            
                            # Run the pymultinest fit.
                            fit.multifit()
                            log.info('  --> Finished pymultinest fit')
                            
                            # Get model evidence and posteriors.
                            evidence = fit.fit_stats()
                            fm_evidence = evidence[0]['nested sampling global log-evidence']  # FM evidence
                            fm_posteriors = evidence[0]['marginals']  # FM posteriors
                            null_evidence = evidence[1]['nested sampling global log-evidence']  # null evidence
                            null_posteriors = evidence[1]['marginals']  # null posteriors
                            evidence_ratio = fm_evidence - null_evidence
                            
                            # Plot the pymultinest fit results.
                            path = os.path.join(output_dir_kl, key + '-corner_c%.0f' % (k + 1) + '.pdf')
                            corn, nullcorn = fit.fit_plots()
                            plt.close()
                            corn
                            plt.savefig(path)
                            plt.close()
                            path = os.path.join(output_dir_kl, key + '-model_c%.0f' % (k + 1) + '.pdf')
                            fit.fm_residuals()
                            plt.savefig(path)
                            plt.close()
                            
                            # Write the pymultinest fit results into a table.
                            flux_jy = fit.fit_flux.bestfit * guess_flux
                            flux_jy *= fzero[filt] / 10**(mstar[filt] / 2.5)  # Jy
                            flux_jy_err = fit.fit_flux.error * guess_flux
                            flux_jy_err *= fzero[filt] / 10**(mstar[filt] / 2.5)  # Jy
                            flux_si = fit.fit_flux.bestfit * guess_flux
                            flux_si *= fzero_si[filt] / 10**(mstar[filt] / 2.5)  # erg/cm^2/s/A
                            flux_si *= 1e-7 * 1e4 * 1e4  # W/m^2/um
                            flux_si_err = fit.fit_flux.error * guess_flux
                            flux_si_err *= fzero_si[filt] / 10**(mstar[filt] / 2.5)  # erg/cm^2/s/A
                            flux_si_err *= 1e-7 * 1e4 * 1e4  # W/m^2/um
                            flux_si_alt = flux_jy * 1e-26 * 299792458. / (1e-6 * self.database.red[key]['CWAVEL'][j])**2 * 1e-6  # W/m^2/um
                            flux_si_alt_err = flux_jy_err * 1e-26 * 299792458. / (1e-6 * self.database.red[key]['CWAVEL'][j])**2 * 1e-6  # W/m^2/um
                            delmag = -2.5 * np.log10(fit.fit_flux.bestfit * guess_flux)  # mag
                            delmag_err = 2.5 / np.log(10.) * fit.fit_flux.error / fit.fit_flux.bestfit  # mag
                            if isinstance(mstar_err, dict):
                                mstar_err_temp = mstar_err[filt]
                            else:
                                mstar_err_temp = mstar_err
                            appmag = mstar[filt] + delmag  # vegamag
                            appmag_err = np.sqrt(mstar_err_temp**2 + delmag_err**2)
                            fitsfile = os.path.join(output_dir_kl, key + '-fitpsf_c%.0f' % (k + 1) + '.fits')
                            tab.add_row((k + 1,
                                         -(fit.fit_x.bestfit - data_centx) * pxsc_arcsec,  # arcsec
                                         fit.fit_x.error * pxsc_arcsec,  # arcsec
                                         (fit.fit_y.bestfit - data_centy) * pxsc_arcsec,  # arcsec
                                         fit.fit_y.error * pxsc_arcsec,  # arcsec
                                         flux_jy,
                                         flux_jy_err,
                                         flux_si,
                                         flux_si_err,
                                         flux_si_alt,
                                         flux_si_alt_err,
                                         fit.fit_flux.bestfit * guess_flux,
                                         fit.fit_flux.error * guess_flux,
                                         delmag,  # mag
                                         delmag_err,  # mag
                                         appmag,  # mag
                                         appmag_err,  # mag
                                         mstar[filt],  # mag
                                         mstar_err,  # mag
                                         np.nan,
                                         evidence_ratio,
                                         scale_factor_avg,
                                         tp_comsubst,
                                         fitsfile))
                            
                            # Write the FM PSF to a file for future plotting.
                            ut.write_fitpsf_images(fit, fitsfile, tab[-1])
                        
                        # Otherwise.
                        else:
                            raise NotImplementedError()
                    
                    # Subtract companion before fitting the next one.
                    if subtract or inject:
                        
                        # Make copy of the original pyKLIP dataset.
                        if k == 0:
                            dataset_orig = copy.deepcopy(dataset)
                        
                        # Subtract companion from pyKLIP dataset. Use offset
                        # PSFs w/o high-pass filtering because this will be
                        # applied by the klip_dataset routine below.
                        if inject:
                            ra = companions[k][0]  # arcsec
                            dec = companions[k][1]  # arcsec
                            con = companions[k][2]
                            inputflux = con * np.array(all_offsetpsfs)  # positive to inject companion
                            fileprefix = 'INJECTED-' + key
                        else:
                            ra = tab[-1]['RA']  # arcsec
                            dec = tab[-1]['DEC']  # arcsec
                            con = tab[-1]['CON']
                            inputflux = -con * np.array(all_offsetpsfs)  # negative to remove companion
                            fileprefix = 'KILLED-' + key
                        sep = np.sqrt(ra**2 + dec**2) / pxsc_arcsec  # pix
                        pa = np.rad2deg(np.arctan2(ra, dec))  # deg
                        thetas = [pa + 90. - all_pa for all_pa in all_pas]
                        fakes.inject_planet(frames=dataset.input, centers=dataset.centers, inputflux=inputflux, astr_hdrs=dataset.wcs, radius=sep, pa=pa, thetas=np.array(thetas), field_dependent_correction=None)
                        
                        # Copy pre-KLIP files.
                        for filepath in filepaths:
                            src = filepath
                            dst = os.path.join(output_dir_pk, os.path.split(src)[1])
                            shutil.copy(src, dst)
                        for psflib_filepath in psflib_filepaths:
                            src = psflib_filepath
                            dst = os.path.join(output_dir_pk, os.path.split(src)[1])
                            shutil.copy(src, dst)
                        
                        # Update content of pre-KLIP files.
                        filenames = dataset.filenames.copy()
                        for l, filename in enumerate(filenames):
                            filenames[l] = filename[:filename.find('_INT')]
                        for filepath in filepaths:
                            ww_file = filenames == os.path.split(filepath)[1]
                            file = os.path.join(output_dir_pk, os.path.split(filepath)[1])
                            hdul = fits.open(file)
                            hdul['SCI'].data = dataset.input[ww_file]
                            hdul.writeto(file, output_verify='fix', overwrite=True)
                            hdul.close()
                        
                        # Update and write observations database.
                        temp = self.database.obs.copy()
                        for l in range(len(self.database.obs[key])):
                            file = os.path.split(self.database.obs[key]['FITSFILE'][l])[1]
                            self.database.obs[key]['FITSFILE'][l] = os.path.join(output_dir_pk, file)
                        file = os.path.split(self.database.red[key]['FITSFILE'][j])[1]
                        file = file[file.find('JWST'):file.find('-KLmodes-all')]
                        file = os.path.join(output_dir_fm, file + '.dat')
                        self.database.obs[key].write(file, format='ascii', overwrite=True)
                        self.database.obs = temp
                        
                        # Reduce companion-subtracted data.
                        mode = self.database.red[key]['MODE'][j]
                        annuli = self.database.red[key]['ANNULI'][j]
                        subsections = self.database.red[key]['SUBSECTS'][j]
                        parallelized.klip_dataset(dataset=dataset,
                                                  mode=mode,
                                                  outputdir=output_dir_fm,
                                                  fileprefix=fileprefix,
                                                  annuli=annuli,
                                                  subsections=subsections,
                                                  movement=1,
                                                  numbasis=klmodes,
                                                  maxnumbasis=maxnumbasis,
                                                  calibrate_flux=False,
                                                  psf_library=dataset.psflib,
                                                  highpass=False,
                                                  verbose=False)
                        head = fits.getheader(self.database.red[key]['FITSFILE'][j], 0)
                        temp = os.path.join(output_dir_fm, fileprefix + '-KLmodes-all.fits')
                        hdul = fits.open(temp)
                        hdul[0].header = head
                        hdul.writeto(temp, output_verify='fix', overwrite=True)
                        hdul.close()
                
                # Update source database.
                self.database.update_src(key, j, tab)
                
                # Restore original pyKLIP dataset.
                if subtract:
                    dataset = dataset_orig
        
        pass

def inject_and_recover(raw_dataset,
                       injection_psf, 
                       injection_seps,
                       injection_pas,
                       injection_spacing,
                       injection_fluxes,
                       klip_args,
                       retrieve_fwhm,
                       true_companions=None):
    '''
    Function to inject synthetic PSFs into a pyKLIP dataset, then perform
    KLIP subtraction, then calculate the flux losses from the KLIP process. 

    Parameters
    ----------
    raw_dataset : pyKLIP dataset
        A pyKLIP dataset which companions will be injected into and KLIP
        will be performed on. 
    injection_psf : 2D-array
        The PSF of the companion to be injected. 
    injection_seps : 1D-array
        List of separations to inject companions at (pixels). 
    injection_pas : 1D-array
        List of position angles to inject companions at (degrees).  
    injection_spacing : int, None
        Spacing between companions injected in a single image. If companions
        are too close then it can pollute the recovered flux. Set to 'None'
        to inject only one companion at a time (pixels).
    injection_fluxes : 1D-array
        Same size as injection_seps, units should correspond to the image
        units. This is the *peak* flux of the injection. 
    klip_args : dict
        Arguments to be passed into the KLIP subtraction process
    retrieve_fwhm : float
        Full-Width Half-Maximum value to estimate the 2D gaussian fit when
        retrieving the companion fluxes. 
    true_companions : list of list of three float, optional
        List of real companions to be masked before computing the raw contrast.
        For each companion, there should be a three element list containing
        [RA offset (pixels), Dec offset (pixels), mask radius (pixels)].
        The default is None.

    Returns
    -------
    all_seps : np.array
        Array containing the separations of all injected 
        companions across all images. 
    all_pas : np.array
        Array containing the position angles of all injected 
        companions across all images. 
    all_inj_fluxes : np.array
        Array containing the injected peak fluxes of all injected 
        companions across all images. 
    all_retr_fluxes : np.array
        Array containing the retrieved peak fluxes of all injected 
        companions across all images. 
    '''

    # Initialise some arrays and quantities
    Nsep = len(injection_seps)
    Npa = len(injection_pas)
    list_of_injected = []
    all_injected = False
    all_seps = []
    all_pas = []
    all_inj_fluxes = []
    all_retr_fluxes = []

    # Ensure provided PSF is normalised to a peak intensity of 1
    injection_psf_norm = injection_psf / np.max(injection_psf)

    # Don't want to inject near any known companions, eliminate any
    # of these positions straight away. 
    if true_companions is not None:
        for tcomp in true_companions:
            tcomp_ra, tcomp_de, tcomp_rad = tcomp
            for i in range(Nsep):
                for j in range(Npa):
                    pos_id = i*Npa+j
                    # Convert position to x-y (RA-DEC) offset in pixels
                    inj_ra = injection_seps[i]*np.sin(np.deg2rad(injection_pas[j])) # pixels
                    inj_de = injection_seps[i]*np.cos(np.deg2rad(injection_pas[j])) # pixels
                    # Calculate distance to companion
                    dist = np.sqrt((tcomp_ra-inj_ra)**2+(tcomp_de-inj_de)**2)
                    #Check if too close, if so, lie to the code and say its already injected
                    if dist < tcomp_rad:
                        list_of_injected += [pos_id]
    if len(list_of_injected) != 0:
        log.info('--> {}/{} source positions not suitable for injection.'.format(len(list_of_injected), 
                                                                             Nsep*Npa))
    else:
        log.info('--> All {} source positions suitable for injection.'.format(Nsep*Npa))
                
    # Want to keep going until a companion has been injected and recovered
    # at each given separation and position angle.
    counter = 1
    remaining_to_inject = (Nsep*Npa) - len(list_of_injected)
    with trange(remaining_to_inject, position=0, leave=True) as t:
        while all_injected == False:
            # Make a copy of the dataset
            dataset = copy.deepcopy(raw_dataset)
            # Define array to keep track of currently injected positions 
            current_injected = [] 
            # Loop over separations
            for i in range(Nsep):
                new_sep = injection_seps[i]
                new_flux = injection_fluxes[i]
                # Loop over position angles
                for j in range(Npa):
                    new_pa = injection_pas[j]

                    # Get specific id for this position
                    pos_id = i*Npa+j
                    if pos_id in list_of_injected:
                        # Already injected at this position, skip
                        continue

                    # Need to check if this position is too close to already
                    # injected positions. By default, assume we want to inject. 
                    inject_flag = True
                    for inj_id in current_injected:
                        # If we don't want to inject more than one companion
                        # per image, then flag to not inject. 
                        if injection_spacing == None:
                            inject_flag=False
                            break

                        # Get separation and PA for injected position
                        inj_j = inj_id % Npa 
                        inj_i = (inj_id - inj_j) // Npa 
                        inj_sep = injection_seps[inj_i]
                        inj_pa = injection_pas[inj_j]
                        inj_flux = injection_fluxes[inj_i]

                        # If something was injected close to the coronagraph
                        # don't inject anything else in this image. 
                        if inj_sep < 5:
                            inject_flag = False
                            break

                        # Calculate distance between this injected position
                        # and the new position we'd also like to inject at.
                        # If object is too close to something that's already
                        # injected, we don't want to inject.
                        dist = np.sqrt(new_sep**2+inj_sep**2
                                -2*new_sep*inj_sep*np.cos(np.deg2rad(inj_pa-new_pa)))
                        if dist < injection_spacing:
                            inject_flag = False
                            break

                        # If the difference in fluxes is too large, don't inject
                        # as this can really affect things. 
                        flux_factor = max(inj_flux, new_flux) / min(inj_flux, new_flux)
                        if flux_factor > 10:
                            inject_flag = False
                            break

                    # If this position survived the filtering, inject into images
                    if inject_flag == True:
                        # Mark as injected in this dataset and overall. 
                        current_injected += [pos_id]
                        list_of_injected += [pos_id]
                        
                        # Injected PSF needs to be a 3D array that matches dataset
                        inj_psf_3d = np.array([injection_psf_norm*new_flux for k in range(dataset.input.shape[0])])
              
                        # Inject the PSF
                        fakes.inject_planet(frames=dataset.input, centers=dataset.centers, inputflux=inj_psf_3d,
                            astr_hdrs=dataset.wcs, radius=new_sep, pa=new_pa, stampsize=65)

            # Figure out how many sources were injected
            Ninjected = len(current_injected)
            t.update(Ninjected)

            # Reroute KLIP printing for our own progress bar
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            sys.stdout = StringIO()
            sys.stderr = StringIO()

            # Still in the while loop, need to run KLIP on the dataset we
            # have injected companions into. 
            fileprefix = 'INJ_ITER{}_{}COMP'.format(counter, Ninjected)
            parallelized.klip_dataset(dataset=dataset,
                                      psf_library=dataset.psflib,
                                      fileprefix=fileprefix,
                                      **klip_args)

            # Restore printing
            sys.stdout = original_stdout
            sys.stderr = original_stderr

            # Now need to recover the flux by fitting a 2D Gaussian, mainly interested in the peak
            # flux so this is an okay approximation. Could improve in the future. 
            klipped_file = klip_args['outputdir'] + fileprefix + '-KLmodes-all.fits'
            with fits.open(klipped_file) as hdul:
                klipped_data = hdul[0].data
                frame_ids = range(klipped_data.shape[0])
                centers = [[hdul[0].header['PSFCENTX'], hdul[0].header['PSFCENTY']] for c in frame_ids]
                # Get fluxes for all companions that were injected, for all KL modes used. 
                for inj_id in current_injected:
                    inj_j = inj_id % Npa 
                    inj_i = (inj_id - inj_j) // Npa 
                    inj_sep = injection_seps[inj_i]
                    inj_pa = injection_pas[inj_j]
                    inj_flux = injection_fluxes[inj_i]
                    
                    # Need to loop over each KL mode individually due to pyKLIP subtleties,
                    # basically the same as what pyKLIP would be doing anyway. 
                    retrieved_fluxes = []
                    for img_i in range(klipped_data.shape[0]):
                        retrieved_flux = fakes.retrieve_planet_flux(frames=klipped_data[img_i], 
                                                                      centers=centers[img_i], 
                                                                      astr_hdrs=dataset.output_wcs[0], 
                                                                      sep=inj_sep, 
                                                                      pa=inj_pa,
                                                                      searchrad=5, 
                                                                      guessfwhm=retrieve_fwhm,
                                                                      guesspeak=inj_flux, 
                                                                      refinefit=True)
                        retrieved_fluxes.append(retrieved_flux)
                    retrieved_fluxes = np.array(retrieved_fluxes) #Convert to numpy array

                    # Flux should never be negative, if it is, assume ~=zero flux retrieved
                    neg_mask = np.where(retrieved_fluxes < 0)
                    retrieved_fluxes[neg_mask]=1e-10 

                    # Need to save things to some arrays
                    all_seps += [inj_sep]
                    all_pas += [inj_pa]
                    all_inj_fluxes += [inj_flux]
                    all_retr_fluxes += [retrieved_fluxes]

            # If a companion has been injected and retrieved at every input position then
            # flag to exit the loop. If not increment the counter and continue.
            if len(list_of_injected) == Nsep*Npa:
                all_injected = True
            else:
                counter += 1

    # Return as numpy arrays
    all_seps = np.array(all_seps)
    all_pas = np.array(all_pas)
    all_inj_fluxes = np.array(all_inj_fluxes)
    all_retr_fluxes = np.squeeze(all_retr_fluxes)

    # Ensure dimensions are correct for all_retr_fluxes if # of different KL modes == 1
    if all_retr_fluxes.ndim == 1:
        all_retr_fluxes = all_retr_fluxes[:, np.newaxis]

    return all_seps, all_pas, all_inj_fluxes, all_retr_fluxes
