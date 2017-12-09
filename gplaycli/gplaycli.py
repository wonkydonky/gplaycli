#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPlay-Cli
Copyleft (C) 2015 Matlink
Hardly based on GooglePlayDownloader https://framagit.org/tuxicoman/googleplaydownloader
Copyright (C) 2013 Tuxicoman

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version.
This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
details.
You should have received a copy of the GNU Affero General Public License along with this program.  If not,
see <http://www.gnu.org/licenses/>.
"""

import sys
import os
import logging
import argparse
import requests
import shutil
import stat
import configparser

from enum import IntEnum
from gpapi.googleplay import GooglePlayAPI
from gpapi.googleplay import LoginError
from google.protobuf.message import DecodeError
from pkg_resources import get_distribution, DistributionNotFound
from pyaxmlparser import APK

from . import util

try:
    import keyring
    HAVE_KEYRING = True
except ImportError:
    HAVE_KEYRING = False


try:
    __version__ = '%s [Python%s] ' % (get_distribution('gplaycli').version, sys.version.split()[0])
except DistributionNotFound:
    __version__ = 'unknown: gplaycli not installed (version in setup.py)'

logger = logging.getLogger(__name__)  # default level is WARNING


class ERRORS(IntEnum):
    OK = 0
    TOKEN_DISPENSER_AUTH_ERROR = 5
    TOKEN_DISPENSER_SERVER_ERROR = 6
    KEYRING_NOT_INSTALLED = 10
    CANNOT_LOGIN_GPLAY = 15


class GPlaycli(object):
    def __init__(self, args=None, credentials=None):
        # no config file given, look for one
        if credentials is None:
            # default local user configs
            cred_paths_list = [
                'gplaycli.conf',
                os.path.expanduser("~") + '/.config/gplaycli/gplaycli.conf',
                '/etc/gplaycli/gplaycli.conf'
            ]
            tmp_list = list(cred_paths_list)
            while not os.path.isfile(tmp_list[0]):
                tmp_list.pop(0)
                if not tmp_list:
                    raise OSError("No configuration file found at %s" % cred_paths_list)
            credentials = tmp_list[0]

        default_values = {}
        self.configparser = configparser.ConfigParser(default_values)
        self.configparser.read(credentials)
        self.config = {key: value for key, value in self.configparser.items("Credentials")}

        self.tokencachefile = os.path.expanduser(self.configparser.get("Cache", "token"))
        self.api = None
        self.token_passed = False

        # default settings, ie for API calls
        if args is None:
            self.yes = False
            self.verbose = False
            self.progress_bar = False
            self.logging_enable = False
            self.device_codename = 'bacon'
            self.addfiles_enable = False

        # if args are passed
        else:
            self.yes = args.yes_to_all
            self.verbose = args.verbose
            if self.verbose:
                logger.setLevel(logging.INFO)
                handler = logging.StreamHandler()
                formatter = logging.Formatter("[%(levelname)s] %(message)s")
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                logger.propagate = False
            logger.info('GPlayCli version %s', __version__)
            logger.info('Configuration file is %s', credentials)
            self.progress_bar = args.progress_bar
            self.set_download_folder(args.update_folder)
            self.logging_enable = args.logging_enable
            self.device_codename = args.device_codename
            self.addfiles_enable = args.addfiles_enable

            if args.token_enable is None:
                self.token_enable = self.configparser.getboolean('Credentials', 'token')
            else:
                self.token_enable = args.token_enable
            if self.token_enable:
                if args.token_url is None:
                    self.token_url = self.configparser.get('Credentials', 'token_url')
                else:
                    self.token_url = args.token_url

                if (args.token_str is None) and (args.gsf_id is None):
                    self.token, self.gsfid = self.retrieve_token()
                elif (args.token_str is not None) and (args.gsf_id is not None):
                    self.token = args.token_str
                    self.gsfid = args.gsf_id
                    self.token_passed = True
                else:  # Either args.token_str or args.gsf_id is None
                    raise TypeError("Token string and GSFID have to be passed at the same time.")

            if self.logging_enable:
                self.success_logfile = "apps_downloaded.log"
                self.failed_logfile = "apps_failed.log"
                self.unavail_logfile = "apps_not_available.log"

    def get_cached_token(self):
        try:
            with open(self.tokencachefile, 'r') as tcf:
                token, gsfid = tcf.readline().split()
                if not token:
                    token = None
                    gsfid = None
        except (IOError, ValueError):  # cache file does not exists or is corrupted
            token = None
            gsfid = None
            logger.error('cache file does not exists or is corrupted')
        return token, gsfid

    def write_cached_token(self, token, gsfid):
        try:
            # creates cachedir if not exists
            cachedir = os.path.dirname(self.tokencachefile)
            if not os.path.exists(cachedir):
                os.mkdir(cachedir)
            with open(self.tokencachefile, 'w') as tcf:
                tcf.write("%s %s" % (token, gsfid))
        except IOError as e:
            err_str = "Failed to write token to cache file: %s %s" % (self.tokencachefile, e.strerror)
            logger.error(err_str)
            raise IOError(err_str)

    def retrieve_token(self, force_new=False):
        token, gsfid = self.get_cached_token()
        if token is not None and not force_new:
            logger.info("Using cached token.")
            return token, gsfid
        logger.info("Retrieving token ...")
        r = requests.get(self.token_url)
        if r.text == 'Auth error':
            print('Token dispenser auth error, probably too many connections')
            sys.exit(ERRORS.TOKEN_DISPENSER_AUTH_ERROR)
        elif r.text == "Server error":
            print('Token dispenser server error')
            sys.exit(ERRORS.TOKEN_DISPENSER_SERVER_ERROR)
        token, gsfid = r.text.split(" ")
        logger.info("Token: %s", token)
        logger.info("GSFId: %s", gsfid)
        self.token = token
        self.gsfid = gsfid
        self.write_cached_token(token, gsfid)
        return token, gsfid

    def set_download_folder(self, folder):
        self.config["download_folder"] = folder

    def connect_to_googleplay_api(self):
        self.api = GooglePlayAPI(device_codename=self.device_codename)
        error = None
        email = None
        password = None
        authSubToken = None
        gsfId = None
        if self.token_enable is False:
            logger.info("Using credentials to connect to API")
            email = self.config["gmail_address"]
            if self.config["gmail_password"]:
                logger.info("Using plaintext password")
                password = self.config["gmail_password"]
            elif self.config["keyring_service"] and HAVE_KEYRING is True:
                password = keyring.get_password(self.config["keyring_service"], email)
            elif self.config["keyring_service"] and HAVE_KEYRING is False:
                print("You asked for keyring service but keyring package is not installed")
                sys.exit(ERRORS.KEYRING_NOT_INSTALLED)
        else:
            if self.token_passed:
                logger.info("Using passed token to connect to API")
            else:
                logger.info("Using auto retrieved token to connect to API")
            authSubToken = self.token
            gsfId = int(self.gsfid, 16)
        try:
            self.api.login(email=email,
                                     password=password,
                                     authSubToken=authSubToken,
                                     gsfId=gsfId)
        except (ValueError, IndexError, LoginError, DecodeError) as ve:  # invalid token or expired
            logger.info("Token has expired or is invalid. Retrieving a new one...")
            self.retrieve_token(force_new=True)
            self.api.login(authSubToken=self.token, gsfId=int(self.gsfid, 16))
        success = True
        return success, error

    def list_folder_apks(self, folder):
        list_of_apks = [filename for filename in os.listdir(folder) if filename.endswith(".apk")]
        return list_of_apks

    def prepare_analyse_apks(self):
        download_folder = self.config["download_folder"]
        list_of_apks = [filename for filename in os.listdir(download_folder) if
                        os.path.splitext(filename)[1] == ".apk"]
        if list_of_apks:
            logger.info("Checking apks ...")
            to_update = self.analyse_local_apks(list_of_apks, download_folder)
            self.prepare_download_updates(to_update)

    def analyse_local_apks(self, list_of_apks, download_folder):
        list_apks_to_update = []
        package_bunch = []
        version_codes = []
        for position, filename in enumerate(list_of_apks):
            filepath = os.path.join(download_folder, filename)
            logger.info("Analyzing %s", filepath)
            a = APK(filepath)
            packagename = a.package
            package_bunch.append(packagename)
            version_codes.append(a.version_code)

        # BulkDetails requires only one HTTP request
        # Get APK info from store
        details = self.api.bulkDetails(package_bunch)
        for detail, packagename, filename, apk_version_code in zip(details, package_bunch, list_of_apks, version_codes):
            store_version_code = detail['versionCode']

            # Compare
            if apk_version_code != "" and int(apk_version_code) < int(store_version_code) and int(
                    store_version_code) != 0:
                # Add to the download list
                list_apks_to_update.append([packagename, filename, int(apk_version_code), int(store_version_code)])

        return list_apks_to_update

    def prepare_download_updates(self, list_apks_to_update):
        if list_apks_to_update:
            pkg_todownload = []

            # Ask confirmation before downloading
            message = "The following applications will be updated :"
            for packagename, filename, apk_version_code, store_version_code in list_apks_to_update:
                message += "\n%s Version : %s -> %s" % (filename, apk_version_code, store_version_code)
                pkg_todownload.append([packagename, filename])
            message += "\n"
            print(message)
            if not self.yes:
                print("\nDo you agree?")
                return_value = input('y/n ?')

            if self.yes or return_value == 'y':
                logger.info("Downloading ...")
                downloaded_packages = self.download_packages(pkg_todownload)
                return_string = ' '.join(downloaded_packages)
                print("Updated: " + return_string[:-1])
        else:
            print("Everything is up to date !")
            sys.exit(ERRORS.OK)

    def download_packages(self, pkg_todownload):
        success_downloads = []
        failed_downloads = []
        unavail_downloads = []

        # case where no filenames have been provided
        for index, pkg in enumerate(pkg_todownload):
            if type(pkg) is str:
                pkg_todownload[index] = (pkg, None)

        # BulkDetails requires only one HTTP request
        # Get APK info from store
        details = self.api.bulkDetails([pkg[0] for pkg in pkg_todownload])
        position = 1
        for detail, item in zip(details, pkg_todownload):
            packagename, filename = item

            logger.info("%s / %s %s", position, len(pkg_todownload), packagename)

            # Check for download folder
            download_folder = self.config["download_folder"]
            if not os.path.isdir(download_folder):
                os.mkdir(download_folder)

            # Download
            try:
                data_dict = self.api.download(packagename, progress_bar=self.progress_bar, expansion_files=self.addfiles_enable)
                success_downloads.append(packagename)
            except IndexError as exc:
                logger.error("Error while downloading %s : %s" % (packagename,
                                                                  "this package does not exist, "
                                                                  "try to search it via --search before"))
                unavail_downloads.append((item, exc))
            except Exception as exc:
                logger.error("Error while downloading %s : %s" % (packagename, exc))
                failed_downloads.append((item, exc))
            else:
                if filename is None:
                    filename = packagename + ".apk"
                filepath = os.path.join(download_folder, filename)

                data = data_dict['data']
                additional_data = data_dict['additionalData']

                try:
                    open(filepath, "wb").write(data)
                    if additional_data:
                        for obb_file in additional_data:
                            obb_filename = "%s.%s.%s.obb" % (obb_file["type"], obb_file["versionCode"], data_dict["docId"])
                            obb_filename = os.path.join(download_folder, obb_filename)
                            open(obb_filename, "wb").write(obb_file["data"])
                except IOError as exc:
                    logger.error("Error while writing %s : %s" % (packagename, exc))
                    failed_downloads.append((item, exc))
            position += 1

        success_items = set(success_downloads)
        failed_items = set([item[0] for item, error in failed_downloads])
        unavail_items = set([item[0] for item, error in unavail_downloads])
        to_download_items = set([item[0] for item in pkg_todownload])

        if self.logging_enable:
            self.write_logfiles(success_items, failed_items, unavail_items)

        self.print_failed(failed_downloads + unavail_downloads)
        return to_download_items - failed_items

    def print_failed(self, failed_downloads):
        # Info message
        if not failed_downloads:
            logger.info("Download complete")
        else:
            message = "A few packages could not be downloaded :"
            for pkg, exception in failed_downloads:
                package_name, filename = pkg
                if filename is not None:
                    message += "\n%s : %s" % (filename, package_name)
                else:
                    message += "\n%s" % package_name
                message += "\n%s\n" % exception
            logger.error(message)

    def search(self, search_string, nb_results, free_only=True, include_headers=True):
        try:
            results = self.api.search(search_string, nb_result=nb_results)
        except IndexError:
            results = []
        if not results:
            print("No result")
            return
        all_results = []
        if include_headers:
            # Name of the columns
            col_names = ["Title", "Creator", "Size", "Downloads", "Last Update", "AppID", "Version", "Rating"]
            all_results.append(col_names)
        # Compute results values
        for result in results:
            # skip that app if it not free
            # or if it's beta (pre-registration)
            if (len(result['offer']) == 0 # beta apps (pre-registration)
                or free_only
                and result['offer'][0]['checkoutFlowRequired'] # not free to download
                ):
                continue
            l = [result['title'],
                 result['author'],
                 util.sizeof_fmt(result['installationSize']),
                 result['numDownloads'],
                 result['uploadDate'],
                 result['docId'],
                 result['versionCode'],
                 "%.2f" % result["aggregateRating"]["starRating"]
                ]
            if len(all_results) < int(nb_results) + 1:
                all_results.append(l)

        if self.verbose:
            # Print a nice table
            col_width = []
            for column_indice in range(len(all_results[0])):
                col_length = max([len("%s" % row[column_indice]) for row in all_results])
                col_width.append(col_length + 2)

            for result in all_results:
                print("".join(str("%s" % item).strip().ljust(col_width[indice]) for indice, item in
                              enumerate(result)))
        return all_results

    def write_logfiles(self, success, failed, unavail):
        for result, logfile in [(success, self.success_logfile),
                                (failed, self.failed_logfile),
                                (unavail, self.unavail_logfile)
                               ]:
            if result:
                with open(logfile, 'w') as _buffer:
                    for package in result:
                        print(package, file=_buffer)


def main():
    parser = argparse.ArgumentParser(description="A Google Play Store Apk downloader and manager for command line")
    parser.add_argument('-V', '--version', action='store_true', dest='version', help='Print version number and exit')
    parser.add_argument('-y', '--yes', action='store_true', dest='yes_to_all', help='Say yes to all prompted questions')
    parser.add_argument('-l', '--list', action='store', dest='list', metavar="FOLDER",
                        type=str, help="List APKS in the given folder, with details")
    parser.add_argument('-s', '--search', action='store', dest='search_string', metavar="SEARCH",
                        type=str, help="Search the given string in Google Play Store")
    parser.add_argument('-P', '--paid', action='store_true', dest='paid',
                        default=False, help='Also search for paid apps')
    parser.add_argument('-n', '--number', action='store', dest='number_results', metavar="NUMBER",
                        type=int, help="For the search option, returns the given number of matching applications")
    parser.add_argument('-d', '--download', action='store', dest='packages_to_download', metavar="AppID", nargs="+",
                        type=str, help="Download the Apps that map given AppIDs")
    parser.add_argument('-a', '--additional-files', action='store_true', dest='addfiles_enable',
                        default=False, help="Enable the download of additional files")
    parser.add_argument('-F', '--file', action='store', dest='load_from_file', metavar="FILE",
                        type=str, help="Load packages to download from file, one package per line")
    parser.add_argument('-u', '--update', action='store', dest='update_folder', metavar="FOLDER",
                        type=str, help="Update all APKs in a given folder")
    parser.add_argument('-f', '--folder', action='store', dest='dest_folder', metavar="FOLDER", nargs=1,
                        type=str, default=".", help="Where to put the downloaded Apks, only for -d command")
    parser.add_argument('-dc', '--device-codename', action='store', dest='device_codename', metavar="DEVICE_CODENAME",
                        type=str, default="bacon", help="The device codename to fake", choices=GooglePlayAPI.getDevicesCodenames())
    parser.add_argument('-ts', '--token-str', action='store', dest='token_str', metavar="TOKEN_STR",
                        type=str, default=None, help="Supply token string by yourself, need to supply GSF_ID at the same time")
    parser.add_argument('-g', '--gsf-id', action='store', dest='gsf_id', metavar="GSF_ID",
                        type=str, default=None, help="Supply GSF_ID by yourself, need to supply token string at the same time")
    parser.add_argument('-t', '--token', action='store_true', dest='token_enable', default=None,
                        help='Instead of classical credentials, use the tokenize version')
    parser.add_argument('-tu', '--token-url', action='store', dest='token_url', metavar="TOKEN_URL",
                        type=str, default=None, help="Use the given tokendispenser URL to retrieve a token")
    parser.add_argument('-v', '--verbose', action='store_true', dest='verbose', help='Be verbose')
    parser.add_argument('-c', '--config', action='store', dest='config', metavar="CONF_FILE", nargs=1,
                        type=str, default=None, help="Use a different config file than gplaycli.conf")
    parser.add_argument('-p', '--progress', action='store_true', dest='progress_bar',
                        help='Prompt a progress bar while downloading packages')
    parser.add_argument('-L', '--log', action='store_true', dest='logging_enable', default=False,
                        help='Enable logging of apps status. Downloaded, failed, not available apps will be written in separate logging files')

    if len(sys.argv) < 2:
        sys.argv.append("-h")

    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    cli = GPlaycli(args, args.config)
    success, error = cli.connect_to_googleplay_api()
    if not success:
        logger.error("Cannot login to GooglePlay ( %s )" % error)
        sys.exit(ERRORS.CANNOT_LOGIN_GPLAY)

    if args.list:
        print(cli.list_folder_apks(args.list))

    if args.update_folder:
        cli.prepare_analyse_apks()

    if args.search_string:
        cli.verbose = True
        nb_results = 10
        if args.number_results:
            nb_results = args.number_results
        cli.search(args.search_string, nb_results, not args.paid)

    if args.load_from_file:
        args.packages_to_download = util.load_from_file(args.load_from_file)

    if args.packages_to_download is not None:
        if args.dest_folder is not None:
            cli.set_download_folder(args.dest_folder[0])
        cli.download_packages(args.packages_to_download)


if __name__ == '__main__':
    main()
