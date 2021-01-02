#!/usr/bin/env python
# encoding: utf-8
"""
Organizes photos and videos into folders using date/time

Created on 2013/02/03
Copyright (c) S. Andrew Ning. All rights reserved.
Original: https://github.com/andrewning/sortphotos

Rewrite on 2021/01/01
Copyright (c) Stephan Schuster. All rights reserved.
Fork: https://github.com/StephanSchuster/sortmedia

"""

# pip install PyExifTool pytz timezonefinder
from typing import Tuple
from datetime import datetime, timedelta
from pytz import timezone, utc
from timezonefinder import TimezoneFinder
import exiftool
import filecmp
import json
import os
import re
import shutil
import sys


EXIFTOOL_EXECUTABLE = os.path.expanduser('~/scripts/exiftool/exiftool')

MEDIA_TYPE_PHOTO = 'photo'
MEDIA_TYPE_VIDEO = 'video'

TAG_DATE_PHOTO = 'EXIF:DateTimeOriginal'
TAG_DATE_VIDEO = 'QuickTime:CreateDate'
TAG_DATE_FILE = 'File:FileModifyDate'
TAG_GPS_LATITUDE = 'Composite:GPSLatitude'
TAG_GPS_LONGITUDE = 'Composite:GPSLongitude'


def format_offset(offset: timedelta) -> str:
    diff_min = int(offset.total_seconds() / 60)
    return '{sign} {hours:02d}:{minutes:02d}'.format(
        sign='-' if diff_min < 0 else '+',
        hours=abs(diff_min) // 60,
        minutes=abs(diff_min) % 60)


def get_offset(lat: float, lng: float, date: datetime) -> timedelta:
    try:
        # get time zone name from GPS posiiton via rough offline map
        tz_name = TimezoneFinder().certain_timezone_at(lng=lng, lat=lat)
        if tz_name is None:
            return None

        # localize given date
        tz = timezone(tz_name)
        date_tz = tz.localize(date)
        date_utc = utc.localize(date)

        # calculate offset between dates
        return date_utc - date_tz

    except Exception as e:
        return None


def parse_date(text: str) -> Tuple[datetime, timedelta]:

    # YYYY:MM:DD HH:MM:SS         --> local time (mostly) or UTC time
    # YYYY:MM:DD HH:MM:SS+HH:MM   --> local time with positive offset
    # YYYY:MM:DD HH:MM:SS-HH:MM   --> local time with negative offset
    # YYYY:MM:DD HH:MM:SSZ        --> indicates UTC time

    # split into date and time
    elements = str(text).strip().split()  # ['YYYY:MM:DD', 'HH:MM:SS+HH:MM']
    if len(elements) < 1:
        return None, None

    # parse date
    date_entries = elements[0].split(':')  # ['YYYY', 'MM', 'DD']
    # check if three entries, nonzero year, and no decimal (occurs for timestamps with only time)
    if len(date_entries) == 3 and date_entries[0] > '0000' and '.' not in ''.join(date_entries):
        year = int(date_entries[0])
        month = int(date_entries[1])
        day = int(date_entries[2])
    else:
        return None, None

    # default time
    hour = 12
    minute = 0
    second = 0

    # default offset
    offset = timedelta(0)

    # parse time and offset
    if len(elements) > 1:
        time_entries = re.split('(\+|-|Z)', elements[1])  # ['HH:MM:SS', '+', 'HH:MM']

        # time
        time = time_entries[0].split(':')  # ['HH', 'MM', 'SS']
        if len(time) == 3:
            hour = int(time[0])
            minute = int(time[1])
            second = int(time[2].split('.')[0])
        elif len(time) == 2:
            hour = int(time[0])
            minute = int(time[1])

        # offset
        if len(time_entries) > 2:
            offset_entries = time_entries[2].split(':')  # ['HH', 'MM']
            if len(offset_entries) == 2:
                offset_hours = int(offset_entries[0])
                offset_minutes = int(offset_entries[1])
                offset = timedelta(hours=offset_hours, minutes=offset_minutes)
                if time_entries[1] == '-':
                    offset *= -1

    # create date object
    try:
        date = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None, None  # most probably caused by errors in time format

    # final sanity checks
    try:
        date.strftime('%Y/%m')  # the concrete format is not relevant here
    except ValueError:
        return None, None  # "valid" dates before 1900 could cause trouble

    return date, offset


def get_date(data: datetime, media_type: str) -> Tuple[datetime, str]:

    # GOAL: return local date/time, not UTC
    #
    # photo:
    # - consider only EXIF:DateTimeOriginal
    #   - de-facto standard
    #   - single source of truth
    #   - specified in local time
    # - no time conversion needed
    #
    # video:
    # - consider only QuickTime:CreateDate
    #   - most commonly used
    #   - single source of truth
    #   - specified in UTC (but often given in local time)
    # - conversion to local time
    #   - calculate UTC offset via GPS position if possible
    #   - use UTC offset from file system after heuristic check
    #   - assume create date is given in local time, not UTC

    if media_type == MEDIA_TYPE_PHOTO:
        if TAG_DATE_PHOTO in data:
            date_photo, _ = parse_date(data[TAG_DATE_PHOTO])
            if date_photo is not None:
                return date_photo, TAG_DATE_PHOTO + ' defined in local time'

    elif media_type == MEDIA_TYPE_VIDEO:
        if TAG_DATE_VIDEO in data:
            date_video, _ = parse_date(data[TAG_DATE_VIDEO])
            if date_video is not None:
                if TAG_GPS_LATITUDE in data and TAG_GPS_LONGITUDE in data:
                    offset_gps = get_offset(data[TAG_GPS_LATITUDE], data[TAG_GPS_LONGITUDE], date_video)
                    if offset_gps is not None:
                        date_video += offset_gps
                        return date_video, TAG_DATE_VIDEO + ' in UTC ' + format_offset(offset_gps) + ' via GPS'
                if TAG_DATE_FILE in data:
                    date_file, offset_file = parse_date(data[TAG_DATE_FILE])
                    if date_file is not None and offset_file is not None:
                        if abs(((date_file - offset_file) - date_video).total_seconds()) <= 3:
                            date_video += offset_file
                            return date_video, TAG_DATE_VIDEO + ' in UTC ' + format_offset(offset_file) + ' via File'
                return date_video, TAG_DATE_VIDEO + ' assumed in local time'

    return None, None


def sort(media_type: str, src_dir: str, dst_dir: str,
         copy: bool, keep: bool, test: bool, verbose: bool, recursive: bool,
         subdir_format: str, filename_format: str, if_condition: str):

   # validate source directory

    if not os.path.exists(src_dir):
        print('Source directory does not exist')
        exit(1)
    if not os.path.isdir(src_dir):
        print('Source path is not a directory')
        exit(1)

    # preprocessing with ExifTool

    args = ['-a', '-G']
    if media_type == MEDIA_TYPE_PHOTO:
        args += ['-' + TAG_DATE_PHOTO]
    elif media_type == MEDIA_TYPE_VIDEO:
        args += ['-' + TAG_DATE_VIDEO, '-' + TAG_DATE_FILE,
                 '-' + TAG_GPS_LATITUDE + '#', '-' + TAG_GPS_LONGITUDE + '#']
    if if_condition:
        args += ['-if', if_condition]
    if recursive:
        args += ['-r']
    args += [src_dir]

    with exiftool.ExifTool(EXIFTOOL_EXECUTABLE) as et:
        print('Preprocessing with ExifTool ...')
        try:
            metadata = et.execute_json(*args)
        except ValueError:
            print('\nNo files to parse or invalid data')
            exit(1)

    if verbose:
        print('\nJSON result of source files read:')
        print(json.dumps(metadata, indent=2))

    # final processing with Python

    print('\nFinal processing with Python ...')

    num_files = len(metadata)
    num_ignored = 0
    num_duplicates = 0
    num_processed = 0
    processed = []

    if test:
        mode = 'TEST'
    elif copy:
        mode = 'COPY'
    else:
        mode = 'MOVE'

    if test:
        test_file_dict = {}

    for idx, data in enumerate(metadata):

        # extract source file
        src_file = data['SourceFile']

        # extract date and info
        date, info = get_date(data, media_type)

        # print progress info/bar
        if verbose:
            print('\n[' + str(idx + 1) + '/' + str(num_files) + '] ' + mode)
            print('Source file: ' + src_file)
        else:
            num_dots = int(20.0 * (idx + 1) / num_files)
            sys.stdout.write('\r[%-20s] %d / %d' % ('=' * num_dots, idx + 1, num_files))
            sys.stdout.flush()

        # ignore paths with .|@|#
        if (src_file.startswith('.') and not src_file.startswith('.' + os.path.sep)) or ((os.path.sep + '.') in src_file):
            if verbose:
                print('Please note: Ignoring file due to special meaning of "." in path.')
            num_ignored += 1
            continue
        if src_file.startswith('@') or ((os.path.sep + '@') in src_file):
            if verbose:
                print('Please note: Ignoring file due to special meaning of "@" in path.')
            num_ignored += 1
            continue
        if src_file.startswith('#') or ((os.path.sep + '#') in src_file):
            if verbose:
                print('Please note: Ignoring file due to special meaning of "#" in path.')
            num_ignored += 1
            continue

        # ignore files without date
        if not date:
            if verbose:
                print('Please note: Ignoring file without valid date in relevant tag(s).')
            num_ignored += 1
            continue

        # print tag and date info
        if verbose:
            print('Tag details: ' + info)
            print('Date & time: ' + str(date))

        # create folder structure
        dst_subdirs_path = date.strftime(subdir_format)
        dst_subdirs = dst_subdirs_path.split('/')
        dst_file = dst_dir
        for dst_subdir in dst_subdirs:
            dst_file = os.path.join(dst_file, dst_subdir)
            if not test and not os.path.exists(dst_file):
                os.makedirs(dst_file)

        # rename file if necessary
        filename = os.path.basename(src_file)
        if filename_format is not None:
            name = date.strftime(filename_format)
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext == '.jpeg':
                ext = '.jpg'
            filename = name + ext

        # setup destination file
        dst_file = os.path.join(dst_file, filename)
        dst_root, dst_ext = os.path.splitext(dst_file)

        # print destination file
        if verbose:
            print('Destination: ' + dst_file)

        # check for collisions
        same_name_appendix = 1
        identical_file_exists = False
        while True:
            same_filename_exists = os.path.isfile(dst_file)
            if (same_filename_exists or (test and dst_file in test_file_dict.keys())):
                if same_filename_exists:
                    dst_compare = dst_file
                else:
                    dst_compare = test_file_dict[dst_file]

                if not keep and filecmp.cmp(src_file, dst_compare):
                    identical_file_exists = True
                    if verbose:
                        print('Please note: Identical file with same name exists in destination.')
                    break
                else:
                    dst_file = dst_root + '_' + str(same_name_appendix) + dst_ext
                    same_name_appendix += 1
                    if verbose:
                        print('Please note: Different file with same name exists in destination.')
                        print('Renaming to: ' + dst_file)
            else:
                break
        if test:
            test_file_dict[dst_file] = src_file

        # finally move or copy the file
        if identical_file_exists:
            num_duplicates += 1
            if not test:
                if copy:
                    continue
                else:
                    os.remove(src_file)
        else:
            num_processed += 1
            processed.append((src_file, dst_file))
            if not test:
                if copy:
                    shutil.copy2(src_file, dst_file)
                else:
                    shutil.move(src_file, dst_file)

    print()
    print(str(num_ignored).rjust(5) + ' files ignored')
    print(str(num_duplicates).rjust(5) + ' duplicates skipped')
    print(str(num_processed).rjust(5) + ' files processed')
    print()

    if num_processed > 0:
        for src, dst in processed:
            print(mode + ': ' + src + ' --> ' + dst)
    else:
        print('No files ' + ('copied' if copy else 'moved') + ' to destination')


def main():
    import argparse

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description='Organizes photos and videos into folders using date/time')
    parser.add_argument('media_type', type=str, choices=['photo', 'video'],
                        help='media type')
    parser.add_argument('src_dir', type=str,
                        help='source directory')
    parser.add_argument('dst_dir', type=str,
                        help='destination directory')
    parser.add_argument('-c', '--copy', action='store_true',
                        help='copy files instead of moving files')
    parser.add_argument('-k', '--keep', action='store_true',
                        help='keep duplicate files after renaming')
    parser.add_argument('-t', '--test', action='store_true',
                        help='dry run without actual changes')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='print some more output to console')
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='search source directory recursively')
    parser.add_argument('-s', '--subdirs', metavar='X', type=str, default='%Y/%m',
                        help='destination subdirectory structure\n* use strftime format codes for dates\n* use forward slashes for subdirectories\n* the default is "%%Y/%%m" (e.g. 2020/02)')
    parser.add_argument('-f', '--filename', metavar='X', type=str, default=None,
                        help='destination file name pattern\n* use strftime format codes for dates\n* the default is "None" (original name)')
    parser.add_argument('-i', '--condition', metavar='X', type=str, default=None,
                        help='if condition passed to ExifTool\n* the default is "None" (use all files)')

    args = parser.parse_args()

    sort(args.media_type, args.src_dir, args.dst_dir,
         args.copy, args.keep, args.test, args.verbose, args.recursive,
         args.subdirs, args.filename, args.condition)


if __name__ == '__main__':
    main()
