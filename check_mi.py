#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from multiprocessing import Process, Queue, cpu_count
from queue import Empty

__author__ = "Paper Folding"
__copyright__ = "Copyright 2025, Paper Folding"
__credits__ = ["Fabiano Tarlao", "Paper Folding"]
__license__ = "GPL3"
__version__ = "0.9.5"
__maintainer__ = "Paper Folding"
__status__ = "Stable"

import argparse
import csv
import os
import sys
import textwrap as _textwrap
import time
import warnings
from math import floor
from subprocess import PIPE, Popen

import ffmpeg
import PIL
from PIL import Image as ImageP
from wand.exceptions import CorruptImageError
from wand.image import Image as ImageW

LICENSE = "Copyright (C) 2018  Fabiano Tarlao.\nThis program comes with ABSOLUTELY NO WARRANTY.\n" \
          "This is free software, and you are welcome to redistribute it under GPL3 license conditions"

UPDATE_SEC_INTERVAL = 5  # sec
UPDATE_MB_INTERVAL = 500  # minimum MBytes of data between output log/messages

# The following extensions includes only the most common ones, you can add other extensions BUT..
# ..BUT, you have to double check Pillow, Imagemagick or FFmpeg to support that format/container
# please in the case I miss important extensions, send a pull request or create an Issue

PIL_EXTENSIONS = ['jpg', 'jpeg', 'jpe', 'png', 'bmp', 'gif', 'pcd', 'tif', 'tiff', 'j2k', 'j2p', 'j2x', 'webp']
PIL_EXTRA_EXTENSIONS = ['eps', 'ico', 'im', 'pcx', 'ppm', 'sgi', 'spider', 'xbm', 'tga']

MAGICK_EXTENSIONS = ['psd', 'xcf']

# this ones are managed by libav or ffmpeg
VIDEO_EXTENSIONS = ['avi', 'mp4', 'mov', 'mpeg', 'mpg', 'm2p', 'mkv', '3gp', 'ogg', 'flv', 'f4v', 'f4p', 'f4a', 'f4b']
AUDIO_EXTENSIONS = ['mp3', 'mp2']

MEDIA_EXTENSIONS = []

CONFIG = None

warnings.filterwarnings("error")  # catch warnings


class MultilineFormatter(argparse.HelpFormatter):
    def _fill_text(self, text, width, indent):
        text = self._whitespace_matcher.sub(' ', text).strip()
        paragraphs = text.split('|n ')
        multiline_text = ''
        for paragraph in paragraphs:
            formatted_paragraph = _textwrap.fill(paragraph, width, initial_indent=indent,
                                                 subsequent_indent=indent) + '\n\n'
            multiline_text = multiline_text + formatted_paragraph
        return multiline_text


def arg_parser():
    epilog_details = """- single file check ignores options -i,-m,-p,-e,-c,-t|n
    - with \'err_detect\' option you can provide the 'strict' shortcut or the flags supported by ffmpeg, e.g.:
    crccheck, bitstream, buffer, explode, or their combination, e.g., +buffer+bitstream|n
    - supported image formats/extensions: """ + str(PIL_EXTENSIONS) + """|n
    - supported image EXTRA formats/extensions:""" + str(PIL_EXTRA_EXTENSIONS + MAGICK_EXTENSIONS) + """|n
    - supported audio/video extensions: """ + str(VIDEO_EXTENSIONS + AUDIO_EXTENSIONS) + """|n
    - output CSV file, has the header raw, and one line for each bad file, providing: file name, error message, file size"""

    parser = argparse.ArgumentParser(description='Checks integrity of Media files (Images, Video, Audio).',
                                     epilog=epilog_details, formatter_class=MultilineFormatter)
    parser.add_argument('checkpath', metavar='P', type=str,
                        help='path to the file or folder')
    parser.add_argument('-c', '--csv', metavar='X', type=str,
                        help='save bad files details on csv file %(metavar)s', dest='csv_filename')
    parser.add_argument('-v', '--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('-r', '--recurse', action='store_true', help='recurse subdirs',
                        dest='is_recurse')
    parser.add_argument('-z', '--enable_zero_detect', metavar='Z', type=int,
                        help='detects when files contain a byte sequence of at least Z equal bytes. This case is '
                             'common for most file formats, jpeg too, you need to set high %(metavar)s values for this '
                             'check to make sense',
                        dest='zero_detect', default=0)
    parser.add_argument('-i', '--disable-images', action='store_true', help='ignore image files',
                        dest='is_disable_image')
    parser.add_argument('-m', '--enable-media', action='store_true', help='enable check for audio/video files',
                        dest='is_enable_media')
    parser.add_argument('-e', '--disable-extra', action='store_true', help='ignore extra image extensions '
                                                                           '(psd, xcf,. and rare ones)',
                        dest='is_disable_extra')
    parser.add_argument('-x', '--err-detect', metavar='E', type=str,
                        help='execute ffmpeg decoding with a specific err_detect flag %(metavar)s, \'strict\' is '
                             'shortcut for +crccheck+bitstream+buffer+explode',
                        dest='error_detect', default='default')
    parser.add_argument('-t', '--threads', metavar='T', type=int,
                        help='number of parallel threads used for speedup, default is (cpu_count / 2 + 1).',
                        dest='threads', default=floor(cpu_count() / 2) + 1)
    parser.add_argument('-T', '--timeout', metavar='K', type=int,
                        help='number of seconds to wait for new performed checks in queue, default is 120 sec, you need'
                             ' to raise the default when working with video files (usually) bigger than few GBytes',
                        dest='timeout', default=120)

    parse_out = parser.parse_args()
    parse_out.enable_csv = parse_out.csv_filename is not None
    return parse_out


def setup(configuration):
    global MEDIA_EXTENSIONS, PIL_EXTENSIONS
    enable_extra = not configuration.is_disable_extra
    enable_images = not configuration.is_disable_image
    enable_media = configuration.is_enable_media

    if enable_extra:
        PIL_EXTENSIONS.extend(PIL_EXTRA_EXTENSIONS)

    if enable_images:
        MEDIA_EXTENSIONS += PIL_EXTENSIONS
        if enable_extra:
            MEDIA_EXTENSIONS += MAGICK_EXTENSIONS

    if enable_media:
        MEDIA_EXTENSIONS += VIDEO_EXTENSIONS + AUDIO_EXTENSIONS


def pil_check(filename):
    img = ImageP.open(filename)  # open the image file
    img.verify()  # verify that it is a good image, without decoding it.. quite fast
    img.close()

    # Image manipulation is mandatory to detect few defects
    img = ImageP.open(filename)  # open the image file
    # alternative (removed) version, decode/recode:
    # f = cStringIO.StringIO()
    # f = io.BytesIO()
    # img.save(f, "BMP")
    # f.close()
    img.transpose(PIL.Image.Transpose.FLIP_LEFT_RIGHT)
    img.close()


def magick_check(filename, flip=True):
    # very useful for xcf, psd and aslo supports pdf
    warnings.filterwarnings("error")  # catch warnings
    try:
        img = ImageW(filename=filename)
        if flip:
            temp = img.flip
        else:
            temp = img.make_blob(format='bmp')
        img.close()
        return temp
    except Warning as w:
        raise CorruptImageError(str(w))
    finally:
        warnings.resetwarnings()


def magick_identify_check(filename):
    proc = Popen(['magick', 'identify', '-regard-warnings', filename], stdout=PIPE,
                 stderr=PIPE)  # '-verbose',
    out, err = proc.communicate()
    exitcode = proc.returncode
    if exitcode != 0:
        raise Exception('Identify error:' + str(exitcode))
    return out


def check_zeros(filename, length_seq_threshold=None):
    f = open(filename, "rb")
    thefilearray = f.read()
    f.close()
    num = 1
    maxnum = num
    prev = None
    maxprev = None
    for i in thefilearray:
        if prev == i:
            num += 1
        else:
            if num > maxnum:
                maxnum = num
                maxprev = prev
            num = 1
            prev = i
    if num > maxnum:
        maxnum = num
    if length_seq_threshold is None:
        return maxnum
    else:
        if maxnum >= length_seq_threshold:
            raise Exception("Equal value sequence, value:", maxprev, "len:", maxnum)


def check_size(filename, zero_exception=True):
    statfile = os.stat(filename)
    filesize = statfile.st_size
    if filesize == 0 and zero_exception:
        raise SyntaxError("Zero size file")
    return filesize


def get_extension(filename):
    file_lowercase = filename.lower()
    return os.path.splitext(file_lowercase)[1][1:]


def is_target_file(filename):
    file_ext = get_extension(filename)
    return file_ext in MEDIA_EXTENSIONS


def ffmpeg_check(filename, error_detect='default', threads=0):
    if error_detect == 'default':
        stream = ffmpeg.input(filename)
    else:
        if error_detect == 'strict':
            custom = '+crccheck+bitstream+buffer+explode'
        else:
            custom = error_detect
        stream = ffmpeg.input(filename, **{'err_detect': custom, 'threads': threads})

    stream = stream.output('pipe:', format="null")
    stream.run(capture_stdout=True, capture_stderr=True)


def save_csv(filename, data):
    with open(filename, mode='w') as out_file:
        out_writer = csv.writer(out_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        for entry in data:
            out_writer.writerow(list(entry))


class TimedLogger:
    def __init__(self):
        self.previous_time = 0
        self.previous_size = 0
        self.start_time = 0

    def start(self):
        self.start_time = self.previous_time = time.time()
        return self

    def print_log(self, num_files, num_bad_files, total_file_size, wait_min_processed=UPDATE_MB_INTERVAL, force=False):
        if not force and (total_file_size - self.previous_size) < wait_min_processed * (1024 * 1024):
            return
        cur_time = time.time()
        from_previous_delta = cur_time - self.previous_time
        if from_previous_delta > UPDATE_SEC_INTERVAL or force:
            self.previous_time = cur_time
            self.previous_size = total_file_size

            from_start_delta = cur_time - self.start_time
            speed_MB = total_file_size / (1024 * 1024 * from_start_delta)
            speed_IS = num_files / from_start_delta
            processed_size_MB = float(total_file_size) / (1024 * 1024)

            print("Number of bad/processed files:", num_bad_files, "/", num_files, ", size of processed files:",
                  "{0:0.1f}".format(processed_size_MB), "MB")
            print("Processing speed:", "{0:0.1f}".format(speed_MB), "MB/s, or", "{0:0.1f}".format(
                speed_IS), "files/s")


def is_pil_simd():
    return 'post' in PIL.__version__


def check_file(filename: str, nominate_file_extension: str = '', error_detect='default', zero_detect=0, ffmpeg_threads=0):
    """check any media integrity

    Args:
        filename (str): file store path
        nominate_file_extension (str, optional): file extension without dot. Defaults to ''.
        error_detect (str, optional): _description_. Defaults to 'default'.
        zero_detect (int, optional): _description_. Defaults to 0.
        ffmpeg_threads (int, optional): _description_. Defaults to 0.

    Returns:
        _type_: _description_
    """
    if sys.version_info[0] < 3:
        filename = filename.decode('utf8')

    file_lowercase = filename.lower()
    file_ext = nominate_file_extension if nominate_file_extension else os.path.splitext(file_lowercase)[1][1:]

    file_size = 'NA'

    try:
        file_size = check_size(filename)
        if zero_detect > 0:
            check_zeros(filename, CONFIG.zero_detect)

        if file_ext in PIL_EXTENSIONS:
            pil_check(filename)
            magick_check(filename)
            magick_identify_check(filename)

        if file_ext in MAGICK_EXTENSIONS:
            magick_check(filename)
            magick_identify_check(filename)

        if file_ext in VIDEO_EXTENSIONS:
            ffmpeg_check(filename, error_detect=error_detect, threads=ffmpeg_threads)

    # except ffmpeg.Error as e:
    #     # print e.stderr
    #     return False, (filename, str(e), file_size)
    except Exception as e:
        # IMHO "Exception" is NOT too broad, io/decode/any problem should be (with details) an image problem
        return False, (filename, str(e), file_size)

    return True, (filename, None, file_size)


def log_check_outcome(check_outcome_detail):
    print("Bad file:", check_outcome_detail[0], ", error detail:", check_outcome_detail[
        1], ", size[bytes]:", check_outcome_detail[2])


def worker(in_queue, out_queue, CONFIG):
    try:
        while True:
            full_filename = in_queue.get(block=True, timeout=2)
            is_success = check_file(full_filename, '', CONFIG.error_detect, zero_detect=CONFIG.zero_detect)
            out_queue.put(is_success)
    except Empty:
        print("Closing parallel worker, the worker has no more tasks to perform")
        return
    except Exception as e:
        print("Parallel worker got unexpected error", str(e))
        sys.exit(1)


def main():
    global CONFIG
    if not is_pil_simd():
        print("********WARNING*******************************************************")
        print("You are using Python Pillow PIL module and not the Pillow-SIMD module.")
        print("Pillow-SIMD is a 4x faster drop-in replacement of the base PIL module.")
        print("Uninstalling Pillow PIL and installing Pillow-SIMD is a good idea.")
        print("**********************************************************************")

    CONFIG = arg_parser()
    setup(CONFIG)
    check_path = CONFIG.checkpath

    print("Files integrity check for:", check_path)

    if os.path.isfile(check_path):
        # manage single file check
        is_success = check_file(check_path, '', CONFIG.error_detect)
        if not is_success[0]:
            check_outcome_detail = is_success[1]
            log_check_outcome(check_outcome_detail)
            sys.exit(1)
        else:
            print("File", check_path, "is OK")
            sys.exit(0)

    # manage folder (searches media files into)

    # initializations
    count = 0
    count_bad = 0
    total_file_size = 0
    bad_files_info = [("file_name", "error_message", "file_size[bytes]")]
    timed_logger = TimedLogger().start()

    task_queue = Queue()
    out_queue = Queue()
    pre_count = 0

    for root, sub_dirs, files in os.walk(check_path):
        media_files = []
        for filename in files:
            if is_target_file(filename):
                media_files.append(filename)
        # thumb check
        thumb = root + os.sep + '.thumb'
        if os.path.exists(thumb):
            try:
                pil_check(thumb)
            except Exception:
                print('Removing thumbnail: ' + thumb)
                os.remove(thumb)

        pre_count += len(media_files)

        for filename in media_files:
            full_filename = os.path.join(root, filename)
            task_queue.put(full_filename)

        if not CONFIG.is_recurse:
            break  # we only check the root folder

    for i in range(CONFIG.threads):
        p = Process(target=worker, args=(task_queue, out_queue, CONFIG))
        p.start()

    # consume the outcome
    try:
        for j in range(pre_count):

            count += 1

            is_success = out_queue.get(block=True, timeout=CONFIG.timeout)
            file_size = is_success[1][2]
            if file_size != 'NA':
                total_file_size += file_size

            if not is_success[0]:
                check_outcome_detail = is_success[1]
                count_bad += 1
                bad_files_info.append(check_outcome_detail)
                log_check_outcome(check_outcome_detail)
                # print "RATIO:", count_bad, "/", count

            # visualization logs and stats
            timed_logger.print_log(count, count_bad, total_file_size)
    except Empty as e:
        print("Waiting other results for too much time, perhaps you have to raise the timeout", e.message)
    print("\n**Task completed**\n")
    timed_logger.print_log(count, count_bad, total_file_size, force=True)

    if count_bad > 0 and CONFIG.enable_csv:
        print("\nSave details for bad files in CSV format, file path:", CONFIG.csv_filename)
        save_csv(CONFIG.csv_filename, bad_files_info)

    if count_bad == 0:
        print("The files are OK :-)")
    else:
        print("Few files look damaged :-(")


if __name__ == "__main__":
    main()
