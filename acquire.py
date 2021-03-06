#!/usr/bin/env python3
from __future__ import print_function
import sys
import os
import numpy as np
import cv2
import time
import ctypes
import multiprocessing
from astropy.coordinates import EarthLocation
from astropy.time import Time
from astropy.io import fits
import astropy.units as u
from stvid.utils import get_sunset_and_sunrise
import logging
import configparser
import argparse
import zwoasi as asi


# Capture images from cv2
def capture_cv2(buf, z1, t1, z2, t2, nx, ny, nz, tend, device_id, live):
    # Array flag
    first = True

    # Initialize device
    device = cv2.VideoCapture(device_id)

    # Set properties
    device.set(3, nx)
    device.set(4, ny)

    # Loop until reaching end time
    while float(time.time()) < tend:
        # Get frames
        for i in range(nz):
            # Store start time
            t0 = float(time.time())

            # Get frame
            res, frame = device.read()

            # Compute mid time
            t = (float(time.time())+t0)/2.0

            # Skip lost frames
            if res is True:
                # Convert image to grayscale
                z = np.asarray(cv2.cvtColor(
                    frame, cv2.COLOR_BGR2GRAY)).astype(np.uint8)

                # Display Frame
                if live is True:
                    cv2.imshow("Capture", z)
                    cv2.waitKey(1)

                # Store results
                if first:
                    z1[i] = z
                    t1[i] = t
                else:
                    z2[i] = z
                    t2[i] = t

        # Assign buffer ready
        if first:
            buf.value = 1
        else:
            buf.value = 2

        # Swap flag
        first = not first

    # End capture
    logger.info("Exiting capture")
    device.release()


# Capture images
def capture_asi(buf, z1, t1, z2, t2, nx, ny, nz, tend, device_id, live, gain,
                maxgain, autogain, exposure, bins, brightness, bandwidth, high_speed):
    # Array flag
    first = True

    # Initialize device
    asi.init(os.getenv("ZWO_ASI_LIB"))

    camera = asi.Camera(device_id)
    camera_info = camera.get_camera_property()
    logger.info('ASI Camera info: %s' % camera_info)
    logger.info(camera_info['MaxHeight'])

    camera.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, bandwidth)
    camera.disable_dark_subtract()
    camera.set_control_value(asi.ASI_GAIN, gain, auto=autogain)
    camera.set_control_value(asi.ASI_EXPOSURE, exposure, auto=False)
    camera.set_control_value(asi.ASI_AUTO_MAX_GAIN, maxgain)
    camera.set_control_value(asi.ASI_WB_B, 99)
    camera.set_control_value(asi.ASI_WB_R, 75)
    camera.set_control_value(asi.ASI_GAMMA, 50)
    camera.set_control_value(asi.ASI_BRIGHTNESS, brightness)
    camera.set_control_value(asi.ASI_FLIP, 0)
    camera.set_control_value(asi.ASI_HIGH_SPEED_MODE, high_speed)
    camera.set_roi(bins=binning)
    if 'HardwareBin' in camera.get_controls():
        camera.set_control_value(asi.ASI_HARDWARE_BIN, 1)
    camera.start_video_capture()
    camera.set_image_type(asi.ASI_IMG_RAW8)

    # Fix autogain
    if autogain:
        while True:
            # Get frame
            z = camera.capture_video_frame()

            # Break on no change in gain
            settings = camera.get_control_values()
            if gain == settings["Gain"]:
                break
            gain = settings["Gain"]
            camera.set_control_value(asi.ASI_GAIN, gain, auto=autogain)

    # Loop until reaching end time
    while float(time.time()) < tend:
        # Get settings
        settings = camera.get_control_values()
        gain = settings["Gain"]
        temp = settings["Temperature"]/10.0
        logging.info("Capturing frame with gain %d, temperature %.1f" % (gain, temp))

        # Set gain
        if autogain:
            camera.set_control_value(asi.ASI_GAIN, gain, auto=autogain)

        # Get frames
        for i in range(nz):
            # Store start time
            t0 = float(time.time())

            # Get frame
            z = camera.capture_video_frame()

            # Compute mid time
            t = (float(time.time())+t0)/2.0

            # Display Frame
            if live is True:
                cv2.imshow("Capture", cv2.resize(z, (nx//2, ny//2)))
                cv2.waitKey(1)

            # Store results
            if first:
                z1[i] = z
                t1[i] = t
            else:
                z2[i] = z
                t2[i] = t

        # Assign buffer ready
        if first:
            buf.value = 1
        else:
            buf.value = 2

        # Swap flag
        first = not first

    # End capture
    logger.info("Exiting capture")
    camera.stop_video_capture()


def compress(buf, z1, t1, z2, t2, nx, ny, nz, tend, path):
    # Flag to keep track of processed buffer
    process_buf = 1

    # Start processing
    while True:
        # Wait for buffer to become available
        while buf.value != process_buf:
            time.sleep(1.0)

        # Process first buffer
        if buf.value == 1:
            t = t1
            z = z1.astype('float32')
            process_buf = 2
        elif buf.value == 2:
            t = t2
            z = z2.astype('float32')
            process_buf = 1

        # Compute statistics
        zmax = np.max(z, axis=0)
        znum = np.argmax(z, axis=0)
        zs1 = np.sum(z, axis=0)-zmax
        zs2 = np.sum(z*z, axis=0)-zmax*zmax
        zavg = zs1/float(nz-1)
        zstd = np.sqrt((zs2-zs1*zavg)/float(nz-2))

        # Convert to float and flip
        zmax = np.flipud(zmax.astype('float32'))
        znum = np.flipud(znum.astype('float32'))
        zavg = np.flipud(zavg.astype('float32'))
        zstd = np.flipud(zstd.astype('float32'))

        # Format time
        nfd = "%s.%03d" % (time.strftime("%Y-%m-%dT%T",
                           time.gmtime(t[0])), int((t[0]-np.floor(t[0]))*1000))
        t0 = Time(nfd, format='isot')
        dt = t-t[0]

        # Generate fits
        fname = "%s.fits" % nfd

        # Format header
        hdr = fits.Header()
        hdr['DATE-OBS'] = "%s" % nfd
        hdr['MJD-OBS'] = t0.mjd
        hdr['EXPTIME'] = dt[-1]-dt[0]
        hdr['NFRAMES'] = nz
        hdr['CRPIX1'] = float(nx)/2.0
        hdr['CRPIX2'] = float(ny)/2.0
        hdr['CRVAL1'] = 0.0
        hdr['CRVAL2'] = 0.0
        hdr['CD1_1'] = 1.0/3600.0
        hdr['CD1_2'] = 0.0
        hdr['CD2_1'] = 0.0
        hdr['CD2_2'] = 1.0/3600.0
        hdr['CTYPE1'] = "RA---TAN"
        hdr['CTYPE2'] = "DEC--TAN"
        hdr['CUNIT1'] = "deg"
        hdr['CUNIT2'] = "deg"
        hdr['CRRES1'] = 0.0
        hdr['CRRES2'] = 0.0
        hdr['EQUINOX'] = 2000.0
        hdr['RADECSYS'] = "ICRS"
        hdr['COSPAR'] = cfg.getint('Common', 'observer_cospar')
        hdr['OBSERVER'] = cfg.get('Common', 'observer_name')
        for i in range(nz):
            hdr['DT%04d' % i] = dt[i]
        for i in range(10):
            hdr['DUMY%03d' % i] = 0.0

        # Write fits file
        hdu = fits.PrimaryHDU(data=np.array([zavg, zstd, zmax, znum]),
                              header=hdr)
        hdu.writeto(os.path.join(path, fname))
        logger.info("Compressed %s" % fname)

        # Exit on end of capture
        if t[-1] > tend:
            break

    # Exiting
    logger.info("Exiting compress")


# Main function
if __name__ == '__main__':

    # Read commandline options
    conf_parser = argparse.ArgumentParser(description='Capture and compress' +
                                                      ' live video frames.')
    conf_parser.add_argument('-c', '--conf_file',
                             help="Specify configuration file. If no file" +
                             " is specified 'configuration.ini' is used.",
                             metavar="FILE")
    conf_parser.add_argument('-t', '--test', action='store_true',
                             help='Testing mode - Start capturing immediately')
    conf_parser.add_argument('-l', '--live', action='store_true',
                             help='Display live image while capturing')

    args = conf_parser.parse_args()

    # Process commandline options and parse configuration
    cfg = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    if args.conf_file:
        cfg.read([args.conf_file])
    else:
        cfg.read('configuration.ini')

    # Testing mode
    if args.test:
        testing = True
    else:
        testing = False

    # Live mode
    if args.live:
        live = True
    else:
        live = False

    # Get camera type
    camera_type = cfg.get('Camera', 'camera_type')

    # Get device id
    device_id = cfg.getint(camera_type, 'device_id')

    # Get camera type
    camera_type = cfg.get('Camera', 'camera_type')

    # Current time
    tnow = Time.now()

    # Get obsid
    t = time.gmtime()
    obsid = "%s_%d/%s" % (time.strftime("%Y%m%d", t), device_id, time.strftime("%H%M%S", t))

    # Generate directory
    if testing:
        path = os.path.join(cfg.get('Common', 'observations_path'), "acquire_test")
    else:
        path = os.path.join(cfg.get('Common', 'observations_path'), obsid)
    if not os.path.exists(path):
        os.makedirs(path)

    # Setup logging
    logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] " +
                                     "[%(levelname)-5.5s]  %(message)s")
    logger = logging.getLogger()

    fileHandler = logging.FileHandler(os.path.join(path, "acquire.log"))
    fileHandler.setFormatter(logFormatter)
    logger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setFormatter(logFormatter)
    logger.addHandler(consoleHandler)
    logger.setLevel(logging.DEBUG)

    # Set location
    loc = EarthLocation(lat=cfg.getfloat('Common', 'observer_lat')*u.deg,
                        lon=cfg.getfloat('Common', 'observer_lon')*u.deg,
                        height=cfg.getfloat('Common', 'observer_el')*u.m)

    if not testing:
        # Reference altitudes
        refalt_set = cfg.getfloat('Control', 'alt_sunset')*u.deg
        refalt_rise = cfg.getfloat('Control', 'alt_sunrise')*u.deg

        # Get sunrise and sunset times
        state, tset, trise = get_sunset_and_sunrise(tnow, loc, refalt_set, refalt_rise)

        # Start/end logic
        if state == "sun never rises":
            logger.info("The sun never rises. Exiting program.")
            sys.exit()
        elif state == "sun never sets":
            logger.info("The sun never sets.")
            tend = tnow+24*u.h
        elif (trise < tset):
            logger.info("The sun is below the horizon.")
            tend = trise
        elif (trise >= tset):
            dt = np.floor((tset-tnow).to(u.s).value)
            logger.info("The sun is above the horizon. Sunset at %s."
                        % tset.isot)
            logger.info("Waiting %.0f seconds." % dt)
            tend = trise
            try:
                time.sleep(dt)
            except KeyboardInterrupt:
                sys.exit()
    else:
        tend = tnow+31.0*u.minute

    logger.info("Starting data acquisition.")
    logger.info("Acquisition will end at "+tend.isot)

    # Get settings
    nx = cfg.getint(camera_type, 'nx')
    ny = cfg.getint(camera_type, 'ny')
    nz = cfg.getint(camera_type, 'nframes')
    if camera_type == "ASI":
        gain = cfg.getint(camera_type, 'gain')
        maxgain = cfg.getint(camera_type, 'maxgain')
        autogain = cfg.getboolean(camera_type, 'autogain')
        exposure = cfg.getint(camera_type, 'exposure')
        binning = cfg.getint(camera_type, 'bin')
        brightness = cfg.getint(camera_type, 'brightness')
        bandwidth = cfg.getint(camera_type, 'bandwidth')
        high_speed = cfg.getint(camera_type, 'high_speed')

    # Initialize arrays
    z1base = multiprocessing.Array(ctypes.c_uint8, nx*ny*nz)
    z1 = np.ctypeslib.as_array(z1base.get_obj()).reshape(nz, ny, nx)
    t1base = multiprocessing.Array(ctypes.c_double, nz)
    t1 = np.ctypeslib.as_array(t1base.get_obj())
    z2base = multiprocessing.Array(ctypes.c_uint8, nx*ny*nz)
    z2 = np.ctypeslib.as_array(z2base.get_obj()).reshape(nz, ny, nx)
    t2base = multiprocessing.Array(ctypes.c_double, nz)
    t2 = np.ctypeslib.as_array(t2base.get_obj())
    buf = multiprocessing.Value('i', 0)

    # Set processes
    pcompress = multiprocessing.Process(target=compress,
                                        args=(buf, z1, t1, z2, t2, nx, ny,
                                              nz, tend.unix, path))
    if camera_type == "CV2":
        pcapture = multiprocessing.Process(target=capture_cv2,
                                           args=(buf, z1, t1, z2, t2,
                                                 nx, ny, nz, tend.unix, device_id, live))
    elif camera_type == "ASI":
        pcapture = multiprocessing.Process(target=capture_asi,
                                           args=(buf, z1, t1, z2, t2,
                                                 nx, ny, nz, tend.unix, device_id, live, gain,
                                                 maxgain, autogain, exposure, binning,
                                                 brightness, bandwidth, high_speed))

    # Start
    pcapture.start()
    pcompress.start()

    # End
    try:
        pcapture.join()
        pcompress.join()
    except KeyboardInterrupt:
        pcapture.terminate()
        pcompress.terminate()

    # Release device
    if live is True:
        cv2.destroyAllWindows()
