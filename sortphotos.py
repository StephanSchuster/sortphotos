#!/usr/bin/env python
# encoding: utf-8
"""
Organizes photos and videos into folders using date/time

Created on 2013/02/03
Copyright (c) S. Andrew Ning. All rights reserved.
Original: https://github.com/andrewning/sortphotos

Updated on 2020/12/28
Copyright (c) Stephan Schuster. All rights reserved.
Fork: https://github.com/StephanSchuster/sortphotos

"""

from __future__ import print_function
from __future__ import with_statement
import subprocess
import os
import sys
import shutil
import filecmp
import re
import locale
from datetime import datetime, timedelta
try:
    import json
except:
    import simplejson as json


# fixing / workarounding issue #120
reload(sys)
sys.setdefaultencoding('utf-8')

# setting locale to the 'local' value
locale.setlocale(locale.LC_ALL, '')

exiftool_location = os.path.expanduser('~/scripts/exiftool/exiftool')


# -------- convenience methods ----------


def parse_date_exif(date_string, use_local_time):
    """
    Extract date info from EXIF data

    YYYY:MM:DD HH:MM:SS
    YYYY:MM:DD HH:MM:SS+HH:MM
    YYYY:MM:DD HH:MM:SS-HH:MM
    YYYY:MM:DD HH:MM:SSZ
    """

    # split into date and time
    elements = str(date_string).strip().split()  # ['YYYY:MM:DD', 'HH:MM:SS']

    if len(elements) < 1:
        return None

    # parse year, month, day
    date_entries = elements[0].split(':')  # ['YYYY', 'MM', 'DD']

    # check if three entries, nonzero data, and no decimal (which occurs for timestamps with only time but no date)
    if len(date_entries) == 3 and date_entries[0] > '0000' and '.' not in ''.join(date_entries):
        year = int(date_entries[0])
        month = int(date_entries[1])
        day = int(date_entries[2])
    else:
        return None

    # parse hour, min, second
    time_zone_adjust = False
    hour = 12  # defaulting to noon if no time data provided
    minute = 0
    second = 0

    if len(elements) > 1:
        # ['HH:MM:SS', '+', 'HH:MM']
        time_entries = re.split('(\+|-|Z)', elements[1])
        time = time_entries[0].split(':')  # ['HH', 'MM', 'SS']

        if len(time) == 3:
            hour = int(time[0])
            minute = int(time[1])
            second = int(time[2].split('.')[0])
        elif len(time) == 2:
            hour = int(time[0])
            minute = int(time[1])

        # adjust for time-zone if needed
        if not use_local_time and len(time_entries) > 2:
            time_zone = time_entries[2].split(':')  # ['HH', 'MM']

            if len(time_zone) == 2:
                time_zone_hour = int(time_zone[0])
                time_zone_min = int(time_zone[1])

                # check if + or -
                if time_entries[1] == '+':
                    time_zone_hour *= -1

                dateadd = timedelta(hours=time_zone_hour,
                                    minutes=time_zone_min)
                time_zone_adjust = True

    # form date object
    try:
        date = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None  # errors in time format

    # try converting it (some "valid" dates are way before 1900 and cannot be parsed by strtime later)
    try:
        # any format with year, month, day, would work here.
        date.strftime('%Y/%m-%b')
    except ValueError:
        return None  # errors in time format

    # adjust for time zone if necessary
    if time_zone_adjust:
        date += dateadd

    return date


def get_oldest_timestamp(data, additional_groups_to_ignore, additional_tags_to_ignore, use_local_time, print_all_tags=False):
    """
    Calculate oldest date and related keys
    """

    # save only the oldest date
    date_available = False
    oldest_date = datetime.now()
    oldest_keys = []

    # save src file
    src_file = data['SourceFile']

    # setup tags to ignore
    ignore_groups = ['ICC_Profile'] + additional_groups_to_ignore
    ignore_tags = ['SourceFile', 'XMP:HistoryWhen'] + additional_tags_to_ignore

    if print_all_tags:
        print('All relevant tags:')

    # run through all keys
    for key in data.keys():

        # check if this key needs to be ignored, or is in the set of tags that must be used
        if (key not in ignore_tags) and (key.split(':')[0] not in ignore_groups) and 'GPS' not in key:

            date = data[key]

            if print_all_tags:
                print(str(key) + ', ' + str(date))

            # (rare) check if multiple dates returned in a list, take the first one which is the oldest
            if isinstance(date, list):
                date = date[0]

            try:
                # check for poor-formed exif data, but allow continuation
                exifdate = parse_date_exif(date, use_local_time)
            except Exception as e:
                exifdate = None

            if exifdate and exifdate < oldest_date:
                date_available = True
                oldest_date = exifdate
                oldest_keys = [key]

            elif exifdate and exifdate == oldest_date:
                oldest_keys.append(key)

    if not date_available:
        oldest_date = None

    if print_all_tags:
        print()

    return src_file, oldest_date, oldest_keys


class ExifTool(object):
    """
    Run ExifTool from Python and keep it open

    http://stackoverflow.com/questions/10075115/call-exiftool-from-a-python-script
    """

    sentinel = '{ready}'

    def __init__(self, executable=exiftool_location):
        self.executable = executable

    def __enter__(self):
        self.process = subprocess.Popen(
            ['perl', self.executable, '-stay_open', 'True',  '-@', '-'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.process.stdin.write(b'-stay_open\nFalse\n')
        self.process.stdin.flush()

    def execute(self, *args):
        args = args + ('-execute\n',)
        self.process.stdin.write(str.join('\n', args).encode('utf-8'))
        self.process.stdin.flush()
        output = ''
        fd = self.process.stdout.fileno()
        while not output.rstrip(' \t\n\r').endswith(self.sentinel):
            increment = os.read(fd, 4096)
            output += increment.decode('utf-8')
        return output.rstrip(' \t\n\r')[:-len(self.sentinel)]

    def get_metadata(self, *args):
        try:
            return json.loads(self.execute(*args))
        except ValueError:
            sys.stdout.write('\nNo files to parse or invalid data\n')
            exit()


# ---------------------------------------


def sortPhotos(src_dir, dest_dir, sort_format, rename_format,
               recursive=False, copy_files=False, verbose=True, test=False, remove_duplicates=True,
               additional_groups_to_ignore=['File'], additional_tags_to_ignore=[],
               use_only_groups=None, use_only_tags=None,
               use_local_time=False, if_condition=None):
    """
    Convenience wrapper around ExifTool based on common usage scenarios
    """

    # some error checking
    if not os.path.exists(src_dir):
        print('Source directory does not exist')
        exit()
    if not os.path.isdir(src_dir):
        print('Source path is not a directory')
        exit()

    # setup arguments to exiftool
    args = ['-j', '-a', '-G']

    # setup if clause for exiftool
    if if_condition and not if_condition.isspace():
        args += ['-if', if_condition]

    # setup tags to ignore
    if use_only_tags is not None:
        additional_groups_to_ignore = []
        additional_tags_to_ignore = []
        for t in use_only_tags:
            args += ['-' + t]
    elif use_only_groups is not None:
        additional_groups_to_ignore = []
        for g in use_only_groups:
            args += ['-' + g + ':Time:All']
    else:
        args += ['-time:all']

    if recursive:
        args += ['-r']

    args += [src_dir]

    # get all metadata
    with ExifTool() as e:
        print('Preprocessing with ExifTool ...')
        sys.stdout.flush()
        metadata = e.get_metadata(*args)

    if verbose:
        print()
        print('JSON result of image files read:')
        print(json.dumps(metadata, indent=2))

    print()
    print('Final processing with Python ...')

    num_files = len(metadata)
    num_ignored = 0
    num_duplicates = 0
    num_processed = 0
    processed = []

    if test:
        mode = 'TEST'
    elif copy_files:
        mode = 'COPY'
    else:
        mode = 'MOVE'

    if test:
        test_file_dict = {}

    # parse output extracting oldest relevant date
    for idx, data in enumerate(metadata):

        # extract timestamp date for photo
        src_file, date, keys = get_oldest_timestamp(
            data, additional_groups_to_ignore, additional_tags_to_ignore, use_local_time)

        # fixes further errors when using unicode characters like "\u20AC"
        src_file.encode('utf-8')

        if verbose:
            # progress info
            print()
            print('[' + str(idx+1) + '/' + str(num_files) + '] ' + mode)
            print('Source: ' + src_file)
        else:
            # progress bar
            numdots = int(20.0*(idx+1)/num_files)
            sys.stdout.write('\r')
            sys.stdout.write('[%-20s] %d / %d ' %
                             ('='*numdots, idx+1, num_files))
            sys.stdout.flush()

        # ignore files and folders starting with '.', '@' or '#'
        if (src_file.startswith('.') and not src_file.startswith('./')) or ((os.path.sep + '.') in src_file):
            if verbose:
                print('Ignoring file due to special meaning of "." in path.')
            num_ignored += 1
            continue
        if src_file.startswith('@') or ((os.path.sep + '@') in src_file):
            if verbose:
                print('Ignoring file due to special meaning of "@" in path.')
            num_ignored += 1
            continue
        if src_file.startswith('#') or ((os.path.sep + '#') in src_file):
            if verbose:
                print('Ignoring file due to special meaning of "#" in path.')
            num_ignored += 1
            continue

        # check if no valid date found
        if not date:
            if verbose:
                print('Ignoring file without valid dates in specified tags.')
            num_ignored += 1
            continue

        if verbose:
            print('Date Time (Tag): ' + str(date) +
                  ' (' + ', '.join(keys) + ')')

        # create folder structure
        dir_structure = date.strftime(sort_format)
        dirs = dir_structure.split('/')
        dest_file = dest_dir
        for thedir in dirs:
            dest_file = os.path.join(dest_file, thedir)
            if not test and not os.path.exists(dest_file):
                os.makedirs(dest_file)

        # rename file if necessary
        filename = os.path.basename(src_file)

        if rename_format is not None and date is not None:
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext == '.jpeg':
                ext = '.jpg'
            filename = date.strftime(rename_format) + ext

        # setup destination file
        dest_file = os.path.join(dest_file, filename)
        root, ext = os.path.splitext(dest_file)

        if verbose:
            print('Destination: ' + dest_file)

        # check for collisions
        append = 1
        fileIsIdentical = False

        while True:
            fileExists = os.path.isfile(dest_file)
            if (fileExists or (test and dest_file in test_file_dict.keys())):  # check for existing name
                if fileExists:
                    dest_compare = dest_file
                else:
                    dest_compare = test_file_dict[dest_file]

                # check for identical files
                if remove_duplicates and filecmp.cmp(src_file, dest_compare):
                    fileIsIdentical = True
                    if verbose:
                        print(
                            'Identical file with same name already exists in destination.')
                    break
                else:  # name is same, but file is different
                    dest_file = root + '_' + str(append) + ext
                    append += 1
                    if verbose:
                        print(
                            'Different file with same name already exists in destination.')
                        print('Renaming to: ' + dest_file)
            else:
                break

        if test:
            test_file_dict[dest_file] = src_file

        # finally move or copy the file
        if fileIsIdentical:
            num_duplicates += 1
            if not test:
                if copy_files:
                    continue  # ignore identical files
                else:
                    os.remove(src_file)
        else:
            num_processed += 1
            processed.append((src_file, dest_file))
            if not test:
                if copy_files:
                    shutil.copy2(src_file, dest_file)
                else:
                    shutil.move(src_file, dest_file)

    print()
    print(str(num_ignored).rjust(5) + ' image files ignored')
    print(str(num_duplicates).rjust(5) + ' duplicates skipped')
    print(str(num_processed).rjust(5) + ' images processed')
    print()

    if num_processed > 0:
        for src, dst in processed:
            print(mode + ': ' + src + ' --> ' + dst)
    else:
        print('No files ' + ('copied' if copy_files else 'moved') + ' to destination')


def main():
    import argparse

    # setup command line parsing
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description='Organizes photos and videos into folders using date/time')
    parser.add_argument('src_dir', type=str, help='source directory')
    parser.add_argument('dest_dir', type=str, help='destination directory')
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='search src_dir recursively')
    parser.add_argument('-c', '--copy', action='store_true',
                        help='copy files instead of move')
    parser.add_argument('-s', '--silent', action='store_true',
                        help='reduce output to minimum')
    parser.add_argument('-t', '--test', action='store_true',
                        help='dry run without actual changes')
    parser.add_argument('--sort', type=str, default='%Y/%m-%b',
                        help='choose destination folder structure using datetime format \n\
    * https://docs.python.org/2/library/datetime.html#strftime-and-strptime-behavior \n\
    * use forward slashes to indicate subdirectoryies (independent of OS convention) \n\
    * the default is "%%Y/%%m-%%b" (e.g. 2012/02-Feb)')
    parser.add_argument('--rename', type=str, default=None,
                        help='rename file using format codes \n\
    * https://docs.python.org/2/library/datetime.html#strftime-and-strptime-behavior \n\
    * the default is None which just uses the original filename')
    parser.add_argument('--keep-duplicates', action='store_true', default=False,
                        help='if file is a duplicate keep it anyway (after renaming)')
    parser.add_argument('--ignore-groups', type=str, nargs='+', default=[],
                        help='a list of tag groups that will be ignored for date informations \n\
    * list of groups/tags: http://www.sno.phy.queensu.ca/~phil/exiftool/TagNames/ \n\
    * by default the group "File" is ignored which contains file timestamp data')
    parser.add_argument('--ignore-tags', type=str, nargs='+', default=[],
                        help='a list of tags that will be ignored for date informations \n\
    * list of groups/tags: http://www.sno.phy.queensu.ca/~phil/exiftool/TagNames/ \n\
    * the full tag name needs to be included (e.g. EXIF:CreateDate)')
    parser.add_argument('--use-only-groups', type=str, nargs='+', default=None,
                        help='specify a restricted set of groups to search for date information (e.g. EXIF)')
    parser.add_argument('--use-only-tags', type=str, nargs='+', default=None,
                        help='specify a restricted set of tags to search for date info (e.g. EXIF:CreateDate)')
    parser.add_argument('--use-local-time', action='store_true', default=False,
                        help='disables time zone adjustements and uses local time instead of UTC time')
    parser.add_argument('--if-condition', type=str, default=None,
                        help='a condition clause passed to ExifTool in order to filter files that get processed')

    # parse command line arguments
    args = parser.parse_args()

    sortPhotos(args.src_dir, args.dest_dir, args.sort, args.rename,
               args.recursive, args.copy, not args.silent, args.test, not args.keep_duplicates,
               args.ignore_groups, args.ignore_tags,
               args.use_only_groups, args.use_only_tags,
               args.use_local_time, args.if_condition)


if __name__ == '__main__':
    main()
