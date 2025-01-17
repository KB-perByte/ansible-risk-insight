# -*- mode:python; coding:utf-8 -*-

# Copyright (c) 2022 IBM Corp. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import yaml
import subprocess
import tempfile
import logging
import glob
import re
import sys
import datetime
import tarfile
from dataclasses import dataclass, field, asdict

from .models import (
    LoadType,
)
from .dependency_finder import find_dependency
from .utils import (
    escape_url,
    install_galaxy_target,
    install_github_target,
    get_installed_metadata,
    get_hash_of_url,
    is_url,
    is_local_path,
)
from .loader import (
    get_target_name,
    remove_subdirectories,
    trim_suffix,
)
from .safe_glob import safe_glob

collection_manifest_json = "MANIFEST.json"
collection_files_json = "FILES.json"
role_meta_main_yml = "meta/main.yml"
role_meta_main_yaml = "meta/main.yaml"
requirements_yml = "requirements.yml"

supported_target_types = [
    LoadType.PROJECT,
    LoadType.COLLECTION,
    LoadType.ROLE,
    LoadType.PLAYBOOK,
]

download_metadata_file = "download_meta.json"


@dataclass
class DownloadMetadata(object):
    name: str = ""
    type: str = ""
    version: str = ""
    author: str = ""
    download_url: str = ""
    download_src_path: str = ""  # path to put tar.gz
    hash: str = ""
    metafile_path: str = ""  # path to manifest.json/meta.yml
    files_json_path: str = ""
    download_timestamp: str = ""
    cache_enabled: bool = False
    cache_dir: str = ""  # path to put cache data
    source_repository: str = ""
    requirements_file: str = ""


@dataclass
class Dependency(object):
    dir: str = ""
    name: str = ""
    metadata: DownloadMetadata = field(default_factory=DownloadMetadata)


@dataclass
class DependencyDirPreparator(object):
    root_dir: str = ""
    source_repository: str = ""
    target_type: str = ""
    target_name: str = ""
    target_version: str = ""
    target_path: str = ""
    target_dependency_dir: str = ""
    target_path_mappings: dict = field(default_factory=dict)
    metadata: DownloadMetadata = field(default_factory=DownloadMetadata)
    download_location: str = ""
    dependency_dir_path: str = ""
    silent: bool = False
    do_save: bool = False
    tmp_install_dir: tempfile.TemporaryDirectory = None

    # -- out --
    dependency_dirs: list = field(default_factory=list)

    def prepare_dir(self, root_install=True, is_src_installed=False, cache_enabled=False, cache_dir=""):
        logging.debug("setup base dirs")
        self.setup_dirs(cache_enabled, cache_dir)
        logging.debug("prepare target dir")
        self.prepare_root_dir(root_install, is_src_installed)
        logging.debug("search dependencies")
        dependencies = find_dependency(self.target_type, self.target_path, self.target_dependency_dir)
        logging.debug("prepare dir for dependencies")
        self.prepare_dependency_dir(dependencies, cache_enabled, cache_dir)
        return self.dependency_dirs

    def setup_dirs(self, cache_enabled=False, cache_dir=""):
        self.download_location = os.path.join(self.root_dir, "archives")
        self.dependency_dir_path = self.root_dir
        # check download_location
        if not os.path.exists(self.download_location):
            os.makedirs(self.download_location)
        # check cache_dir
        if cache_enabled and not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        # check dependency_dir_path
        if not os.path.exists(self.dependency_dir_path):
            os.makedirs(self.dependency_dir_path)
        return

    def prepare_root_dir(self, root_install=True, is_src_installed=False):
        # install root
        if is_src_installed:
            pass
        else:
            # if a project target is a local path, then skip install
            if self.target_type == LoadType.PROJECT and not is_url(self.target_name):
                root_install = False

            # if a collection/role is a local path, then skip install (require MANIFEST.json or meta/main.yml to get the actual name)
            if self.target_type in [LoadType.COLLECTION, LoadType.ROLE] and is_local_path(self.target_name):
                root_install = False

            if root_install:
                self.src_install()
                if not self.silent:
                    logging.debug("install() done")
            else:
                download_url = ""
                version = ""
                hash = ""
                download_url, version = get_installed_metadata(self.target_type, self.target_name, self.target_path, self.target_dependency_dir)
                if download_url != "":
                    hash = get_hash_of_url(download_url)
                self.metadata.download_url = download_url
                self.metadata.version = version
                self.metadata.hash = hash
        return

    def prepare_dependency_dir(self, dependencies, cache_enabled=False, cache_dir=""):
        col_dependencies = dependencies.get("dependencies", {}).get("collections", [])
        role_dependencies = dependencies.get("dependencies", {}).get("roles", [])

        col_dependency_dirs = dependencies.get("paths", {}).get("collections", {})
        role_dependency_dirs = dependencies.get("paths", {}).get("roles", {})

        col_dependency_metadata = dependencies.get("metadata", {}).get("collections", {})
        # role_dependency_metadata = dependencies.get("metadata", {}).get("roles", {})

        # TODO: if requirements.yml is provided, download dependencies using it.

        for cdep in col_dependencies:
            col_name = cdep
            col_version = ""
            if type(cdep) is dict:
                col_name = cdep.get("name", "")
                col_version = cdep.get("version", "")
                if col_name == "":
                    col_name = cdep.get("source", "")

            logging.debug("prepare dir for {}:{}".format(col_name, col_version))
            downloaded_dep = Dependency(
                name=col_name,
            )
            downloaded_dep.metadata.type = LoadType.COLLECTION
            downloaded_dep.metadata.name = col_name
            downloaded_dep.metadata.cache_enabled = cache_enabled
            sub_dependency_dir_path = os.path.join(
                self.dependency_dir_path,
                "collections",
                "src",
            )

            if not os.path.exists(sub_dependency_dir_path):
                os.makedirs(sub_dependency_dir_path)

            if cache_enabled:
                logging.debug("cache enabled")
                # TODO: handle version
                is_exist, targz_file = self.is_download_file_exist(LoadType.COLLECTION, col_name, cache_dir)
                # check cache data
                if is_exist:
                    logging.debug("found cache data {}".format(targz_file))
                    metadata_file = os.path.join(targz_file.rsplit("/", 1)[0], download_metadata_file)
                    md = self.find_target_metadata(LoadType.COLLECTION, metadata_file, col_name)
                    downloaded_dep.metadata = md
                else:
                    # if no cache data, download
                    logging.debug("cache data not found")
                    cache_location = os.path.join(cache_dir, "collection", col_name)
                    install_msg = self.download_galaxy_collection(col_name, cache_location, col_version, self.source_repository)
                    metadata = self.extract_collections_metadata(install_msg, cache_location)
                    metadata_file = self.export_data(metadata, cache_location, download_metadata_file)
                    md = self.find_target_metadata(LoadType.COLLECTION, metadata_file, col_name)
                    downloaded_dep.metadata = md
                    if md:
                        targz_file = md.download_src_path
                # install collection from tar.gz
                self.install_galaxy_collection_from_targz(targz_file, sub_dependency_dir_path)
                downloaded_dep.metadata.cache_dir = targz_file
                parts = col_name.split(".")
                downloaded_dep.dir = os.path.join(sub_dependency_dir_path, "ansible_collections", parts[0], parts[1])
            elif col_name in col_dependency_dirs:
                logging.debug("use the specified dependency dirs")
                sub_dependency_dir_path = col_dependency_dirs[col_name]
                col_galaxy_data = col_dependency_metadata.get(col_name, {})
                if isinstance(col_galaxy_data, dict):
                    download_url = col_galaxy_data.get("download_url", "")
                    hash = ""
                    if download_url:
                        hash = get_hash_of_url(download_url)
                    version = col_galaxy_data.get("version", "")
                    downloaded_dep.metadata.source_repository = self.source_repository
                    downloaded_dep.metadata.download_url = download_url
                    downloaded_dep.metadata.hash = hash
                    downloaded_dep.metadata.version = version
                    downloaded_dep.dir = sub_dependency_dir_path
            else:
                logging.debug("download dependency {}".format(col_name))
                is_exist, targz = self.is_download_file_exist(
                    LoadType.COLLECTION, col_name, os.path.join(self.download_location, "collection", col_name)
                )
                if is_exist:
                    metadata_file = os.path.join(self.download_location, "collection", self.target_name, download_metadata_file)
                    self.install_galaxy_collection_from_targz(targz, sub_dependency_dir_path)
                    md = self.find_target_metadata(LoadType.COLLECTION, metadata_file, col_name)
                else:
                    # check download_location
                    sub_download_location = os.path.join(self.download_location, "collection", col_name)
                    if not os.path.exists(sub_download_location):
                        os.makedirs(sub_download_location)
                    install_msg = self.download_galaxy_collection(col_name, sub_download_location, col_version, self.source_repository)
                    metadata = self.extract_collections_metadata(install_msg, sub_download_location)
                    metadata_file = self.export_data(metadata, sub_download_location, download_metadata_file)
                    md = self.find_target_metadata(LoadType.COLLECTION, metadata_file, col_name)
                    if md:
                        self.install_galaxy_collection_from_reqfile(md.requirements_file, sub_dependency_dir_path)
                    # self.install_galaxy_collection_from_targz(md.download_src_path, sub_dependency_dir_path)
                if md is not None:
                    downloaded_dep.metadata = md
                downloaded_dep.metadata.source_repository = self.source_repository
                parts = col_name.split(".")
                downloaded_dep.dir = os.path.join(sub_dependency_dir_path, "ansible_collections", parts[0], parts[1])
            self.dependency_dirs.append(asdict(downloaded_dep))

        for rdep in role_dependencies:
            target_version = None
            if isinstance(rdep, dict):
                rdep_name = rdep.get("name", None)
                target_version = rdep.get("version", None)
                rdep = rdep_name
            name = rdep
            if type(rdep) is dict:
                name = rdep.get("name", "")
                if name == "":
                    name = rdep.get("src", "")
            logging.debug("prepare dir for {}".format(name))
            downloaded_dep = Dependency(
                name=name,
            )
            downloaded_dep.metadata.type = LoadType.ROLE
            downloaded_dep.metadata.name = name
            downloaded_dep.metadata.cache_enabled = cache_enabled
            # sub_dependency_dir_path = "{}/{}".format(dependency_dir_path, rdep)

            sub_dependency_dir_path = os.path.join(
                self.dependency_dir_path,
                "roles",
                "src",
                name,
            )

            if not os.path.exists(sub_dependency_dir_path):
                os.makedirs(sub_dependency_dir_path)
            if cache_enabled:
                logging.debug("cache enabled")
                cache_dir_path = os.path.join(
                    cache_dir,
                    "roles",
                    "src",
                    name,
                )
                if os.path.exists(cache_dir_path) and len(os.listdir(cache_dir_path)) != 0:
                    logging.debug("cache data found")
                    metadata_file = os.path.join(cache_dir_path, download_metadata_file)
                    md = self.find_target_metadata(LoadType.ROLE, metadata_file, self.target_name)
                else:
                    logging.debug("cache data not found")
                    install_msg = install_galaxy_target(name, LoadType.ROLE, cache_dir_path, self.source_repository, target_version)
                    logging.debug("role install msg: {}".format(install_msg))
                    metadata = self.extract_roles_metadata(install_msg)
                    metadata_file = self.export_data(metadata, cache_dir_path, download_metadata_file)
                    md = self.find_target_metadata(LoadType.ROLE, metadata_file, self.target_name)
                self.move_src(sub_dependency_dir_path, cache_dir_path)
                if md is not None:
                    downloaded_dep.metadata = md
            elif name in role_dependency_dirs:
                logging.debug("use the specified dependency dirs")
                sub_dependency_dir_path = role_dependency_dirs[name]
            else:
                is_exist, _ = self.is_download_file_exist(LoadType.ROLE, name, os.path.join(self.download_location, "role", name))
                if is_exist:
                    metadata_file = os.path.join(self.download_location, "role", name, download_metadata_file)
                    md = self.find_target_metadata(LoadType.ROLE, metadata_file, name)
                    self.move_src(md.download_src_path, sub_dependency_dir_path)
                else:
                    install_msg = install_galaxy_target(name, LoadType.ROLE, sub_dependency_dir_path, self.source_repository)
                    logging.debug("role install msg: {}".format(install_msg))
                    metadata = self.extract_roles_metadata(install_msg)
                    sub_download_location = os.path.join(self.download_location, "role", name)
                    metadata_file = self.export_data(metadata, sub_download_location, download_metadata_file)
                    md = self.find_target_metadata(LoadType.ROLE, metadata_file, name)
                if md is not None:
                    downloaded_dep.metadata = md
            downloaded_dep.metadata.source_repository = self.source_repository
            downloaded_dep.dir = sub_dependency_dir_path
            self.dependency_dirs.append(asdict(downloaded_dep))
        return

    def src_install(self):
        try:
            self.setup_tmp_dir()
            self.root_install(self.tmp_install_dir)
        finally:
            self.clean_tmp_dir()
        return

    def root_install(self, tmp_src_dir):
        tmp_src_dir = os.path.join(self.tmp_install_dir.name, "src")

        logging.debug("root type is {}".format(self.target_type))
        if self.target_type == LoadType.PROJECT:
            # install_type = "github"
            # ansible-galaxy install
            if not self.silent:
                print("cloning {} from github".format(self.target_name))
            install_msg = install_github_target(self.target_name, tmp_src_dir)
            if not self.silent:
                logging.debug("STDOUT: {}".format(install_msg))
            # if self.target_dependency_dir == "":
            #     raise ValueError("dependency dir is required for project type")
            dependency_dir = self.target_dependency_dir
            dst_src_dir = os.path.join(self.target_path_mappings["src"], escape_url(self.target_name))
            self.metadata.download_url = self.target_name
        elif self.target_type == LoadType.COLLECTION:
            sub_download_location = os.path.join(self.download_location, "collection", self.target_name)
            install_msg = self.download_galaxy_collection(self.target_name, sub_download_location, version=self.target_version)
            metadata = self.extract_collections_metadata(install_msg, sub_download_location)
            metadata_file = self.export_data(metadata, sub_download_location, download_metadata_file)
            md = self.find_target_metadata(LoadType.COLLECTION, metadata_file, self.target_name)
            self.install_galaxy_collection_from_reqfile(md.requirements_file, tmp_src_dir)
            dst_src_dir = self.target_path_mappings["src"]
            dependency_dir = tmp_src_dir
            self.metadata = md
        elif self.target_type == LoadType.ROLE:
            sub_download_location = os.path.join(self.download_location, "role", self.target_name)
            install_msg = install_galaxy_target(
                self.target_name, self.target_type, tmp_src_dir, self.source_repository, target_version=self.target_version
            )
            logging.debug("role install msg: {}".format(install_msg))
            metadata = self.extract_roles_metadata(install_msg)
            metadata_file = self.export_data(metadata, sub_download_location, download_metadata_file)
            md = self.find_target_metadata(LoadType.ROLE, metadata_file, self.target_name)
            dependency_dir = tmp_src_dir
            dst_src_dir = self.target_path_mappings["src"]
            self.metadata.download_src_path = "{}.{}".format(dst_src_dir, self.target_name)
            self.metadata = md
        else:
            raise ValueError("unsupported container type")

        self.install_log = install_msg
        if self.do_save:
            self.__save_install_log()

        self.set_index(dependency_dir)

        if not self.silent:
            print("moving index")
            logging.debug("index: {}".format(json.dumps(self.index)))
        if self.do_save:
            self.__save_index()
        if not os.path.exists(dst_src_dir):
            os.makedirs(dst_src_dir)
        self.move_src(tmp_src_dir, dst_src_dir)
        root_dst_src_path = "{}/{}".format(dst_src_dir, self.target_name)
        if self.target_type == LoadType.ROLE:
            self.update_role_download_src(metadata_file, dst_src_dir)
            self.metadata.download_src_path = root_dst_src_path
            self.metadata.metafile_path, _ = self.get_metafile_in_target(self.target_type, root_dst_src_path)
            self.metadata.author = self.get_author(self.target_type, self.metadata.metafile_path)

        if self.target_type == LoadType.PROJECT and self.target_dependency_dir:
            dst_dependency_dir = self.target_path_mappings["dependencies"]
            if not os.path.exists(dst_dependency_dir):
                os.makedirs(dst_dependency_dir)
            self.move_src(dependency_dir, dst_dependency_dir)
            logging.debug("root metadata: {}".format(json.dumps(asdict(self.metadata))))
        return

    def set_index(self, path):
        if not self.silent:
            print("crawl content")
        dep_type = LoadType.UNKNOWN
        target_path_list = []
        if os.path.isfile(path):
            # need further check?
            dep_type = LoadType.PLAYBOOK
            target_path_list.append = [path]
        elif os.path.exists(os.path.join(path, collection_manifest_json)):
            dep_type = LoadType.COLLECTION
            target_path_list = [path]
        elif os.path.exists(os.path.join(path, role_meta_main_yml)):
            dep_type = LoadType.ROLE
            target_path_list = [path]
        else:
            dep_type, target_path_list = find_ext_dependencies(path)

        if not self.silent:
            logging.info('the detected target type: "{}", found targets: {}'.format(self.target_type, len(target_path_list)))

        if self.target_type not in supported_target_types:
            logging.error("this target type is not supported")
            sys.exit(1)

        list = []
        for target_path in target_path_list:
            ext_name = get_target_name(dep_type, target_path)
            list.append(
                {
                    "name": ext_name,
                    "type": dep_type,
                }
            )

        index_data = {
            "dependencies": list,
            "path_mappings": self.target_path_mappings,
        }

        self.index = index_data

    def download_galaxy_collection(self, target, output_dir, version="", source_repository=""):
        server_option = ""
        if source_repository:
            server_option = "--server {}".format(source_repository)
        target_version = target
        if version:
            target_version = "{}:{}".format(target, version)
        proc = subprocess.run(
            "ansible-galaxy collection download '{}' {} -p {}".format(target_version, server_option, output_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        install_msg = proc.stdout
        logging.debug("STDOUT: {}".format(install_msg))
        return install_msg

    def download_galaxy_collection_from_reqfile(self, requirements, output_dir, source_repository=""):
        server_option = ""
        if source_repository:
            server_option = "--server {}".format(source_repository)
        proc = subprocess.run(
            "ansible-galaxy collection download -r {} {} -p {}".format(requirements, server_option, output_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        install_msg = proc.stdout
        logging.debug("STDOUT: {}".format(install_msg))
        # return proc.stdout

    def install_galaxy_collection_from_targz(self, tarfile, output_dir):
        logging.debug("install collection from {}".format(tarfile))
        proc = subprocess.run(
            "ansible-galaxy collection install {} -p {}".format(tarfile, output_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        install_msg = proc.stdout
        logging.debug("STDOUT: {}".format(install_msg))
        # return proc.stdout

    def install_galaxy_collection_from_reqfile(self, requirements, output_dir):
        logging.debug("install collection from {}".format(requirements))
        src_dir = requirements.replace(requirements_yml, "")
        proc = subprocess.run(
            "cd {} && ansible-galaxy collection install -r {} -p {}".format(src_dir, requirements, output_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        install_msg = proc.stdout
        logging.debug("STDOUT: {}".format(install_msg))
        # return proc.stdout

    def is_download_file_exist(self, type, target, dir):
        is_exist = False
        filename = ""
        download_metadata_files = glob.glob(os.path.join(dir, type, "**", download_metadata_file), recursive=True)
        # check if tar.gz file already exists
        if len(download_metadata_files) != 0:
            for metafile in download_metadata_files:
                md = self.find_target_metadata(type, metafile, target)
                if md is not None:
                    is_exist = True
                    filename = md.download_src_path
        else:
            namepart = target.replace(".", "-")
            for file in os.listdir(dir):
                if file.endswith(".tar.gz") and namepart in file:
                    is_exist = True
                    filename = file
        return is_exist, filename

    def install_galaxy_role_from_reqfile(self, file, output_dir):
        proc = subprocess.run(
            "ansible-galaxy role install -r {} -p {}".format(file, output_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        install_msg = proc.stdout
        logging.debug("STDOUT: {}".format(install_msg))

    def extract_collections_metadata(self, log_message, download_location):
        # -- log message
        # Downloading collection 'community.rabbitmq:1.2.3' to
        # Downloading https://galaxy.ansible.com/download/ansible-posix-1.4.0.tar.gz to ...
        download_url_pattern = r"Downloading (.*) to"
        url = ""
        version = ""
        hash = ""
        match_messages = re.findall(download_url_pattern, log_message)
        metadata_list = []
        for m in match_messages:
            metadata = DownloadMetadata()
            metadata.type = LoadType.COLLECTION
            if m.endswith("tar.gz"):
                logging.debug("extracted url from download log message: {}".format(m))
                url = m
                version = url.split("-")[-1].replace(".tar.gz", "")
                name = "{}.{}".format(url.split("-")[0].split("/")[-1], url.split("-")[1])
                metadata.download_url = url
                metadata.version = version
                metadata.name = name
                filename = url.split("/")[-1]
                fullpath = "{}/{}".format(download_location, filename)
                if not os.path.exists(fullpath):
                    logging.warning("failed to get metadata for {}".format(url))
                    pass
                m_time = os.path.getmtime(fullpath)
                dt_m = datetime.datetime.utcfromtimestamp(m_time).isoformat()
                metadata.download_timestamp = dt_m
                metadata.download_src_path = fullpath
                metadata.metafile_path, metadata.files_json_path = self.get_metafile_in_target(LoadType.COLLECTION, fullpath)
                metadata.author = self.get_author(LoadType.COLLECTION, metadata.metafile_path)
                metadata.requirements_file = "{}/{}".format(download_location, requirements_yml)

                if url != "":
                    hash = get_hash_of_url(url)
                    metadata.hash = hash
                logging.debug("metadata: {}".format(json.dumps(asdict(metadata))))

                metadata_list.append(asdict(metadata))
        result = {"collections": metadata_list}
        return result

    def extract_roles_metadata(self, log_message):
        # - downloading role from https://github.com/rhythmictech/ansible-role-awscli/archive/1.0.3.tar.gz
        # - extracting rhythmictech.awscli to /private/tmp/role-test/rhythmictech.awscli
        url = ""
        version = ""
        hash = ""
        metadata_list = []
        messages = log_message.splitlines()
        for i, line in enumerate(messages):
            if line.startswith("- downloading role from "):
                metadata = DownloadMetadata()
                metadata.type = LoadType.ROLE
                url = line.split(" ")[-1]
                logging.debug("extracted url from download log message: {}".format(url))
                version = url.split("/")[-1].replace(".tar.gz", "")
                name = messages[i + 1].split("/")[-1]
                metadata.download_url = url
                metadata.version = version
                metadata.name = name
                role_dir = messages[i + 1].split(" ")[-1]
                m_time = os.path.getmtime(role_dir)
                dt_m = datetime.datetime.utcfromtimestamp(m_time).isoformat()
                metadata.download_timestamp = dt_m
                metadata.download_src_path = role_dir
                if url != "":
                    hash = get_hash_of_url(url)
                    metadata.hash = hash
                logging.debug("metadata: {}".format(json.dumps(asdict(metadata))))
                metadata_list.append(asdict(metadata))
        result = {"roles": metadata_list}
        return result

    def find_target_metadata(self, type, metadata_file, target):
        with open(metadata_file, "r") as f:
            metadata = json.load(f)
        if type == LoadType.COLLECTION:
            metadata_list = metadata.get("collections", [])
        elif type == LoadType.ROLE:
            metadata_list = metadata.get("roles", [])
        else:
            logging.warning("metadata not found: {}".format(target))
            return None
        for data in metadata_list:
            dm = DownloadMetadata(**data)
            if dm.name == target:
                logging.debug("found metadata: {}".format(target))
                return dm

    def existing_dependency_dir_loader(self, dependency_type, dependency_dir_path):
        search_dirs = []
        if dependency_type == LoadType.COLLECTION:
            base_dir = dependency_dir_path
            if os.path.exists(os.path.join(dependency_dir_path, "ansible_collections")):
                base_dir = os.path.join(dependency_dir_path, "ansible_collections")
            namespaces = [ns for ns in os.listdir(base_dir) if not ns.endswith(".info")]
            for ns in namespaces:
                colls = [{"name": f"{ns}.{name}", "path": os.path.join(base_dir, ns, name)} for name in os.listdir(os.path.join(base_dir, ns))]
                search_dirs.extend(colls)

        dependency_dirs = []
        for dep_info in search_dirs:
            downloaded_dep = {"dir": "", "metadata": {}}
            downloaded_dep["dir"] = dep_info["path"]
            # meta data
            downloaded_dep["metadata"]["type"] = LoadType.COLLECTION
            downloaded_dep["metadata"]["name"] = dep_info["name"]
            dependency_dirs.append(downloaded_dep)
        return dependency_dirs

    def __save_install_log(self):
        tmpdir = self.tmp_install_dir.name
        tmp_install_log = os.path.join(tmpdir, "install.log")
        with open(tmp_install_log, "w") as f:
            f.write(self.install_log)

    def __save_index(self):
        index_location = self.__path_mappings["index"]
        index_dir = os.path.dirname(os.path.abspath(index_location))
        if not os.path.exists(index_dir):
            os.makedirs(index_dir)
        with open(index_location, "w") as f:
            json.dump(self.index, f, indent=2)

    def move_src(self, src, dst):
        if src == "" or not os.path.exists(src) or not os.path.isdir(src):
            raise ValueError("src {} is not directory".format(src))
        if dst == "" or ".." in dst:
            raise ValueError("dst {} is invalid".format(dst))
        # we use cp command here because shutil module is slow,
        # but the behavior of cp command is slightly different between Mac and Linux
        # we use a command like `cp -r <src>/* <dst>/` so the behavior will be the same
        os.system("cp -r {}/* {}/".format(src, dst))
        return

    def setup_tmp_dir(self):
        if self.tmp_install_dir is None or not os.path.exists(self.tmp_install_dir.name):
            self.tmp_install_dir = tempfile.TemporaryDirectory()

    def clean_tmp_dir(self):
        if self.tmp_install_dir is not None and os.path.exists(self.tmp_install_dir.name):
            self.tmp_install_dir.cleanup()
            self.tmp_install_dir = None

    def export_data(self, data, dir, filename):
        if not os.path.exists(dir):
            os.makedirs(dir)
        file = os.path.join(dir, filename)
        logging.debug("export data {} to {}".format(data, file))
        with open(file, "w") as f:
            json.dump(data, f)
        return file

    def get_metafile_in_target(self, type, filepath):
        metafile_path = ""
        files_path = ""
        if type == LoadType.COLLECTION:
            # get manifest.json
            with tarfile.open(name=filepath, mode="r") as tar:
                for info in tar.getmembers():
                    if info.name.endswith(collection_manifest_json):
                        f = tar.extractfile(info)
                        metafile_path = filepath.replace(".tar.gz", "-{}".format(collection_manifest_json))
                        with open(metafile_path, "wb") as c:
                            c.write(f.read())
                    if info.name.endswith(collection_files_json):
                        f = tar.extractfile(info)
                        files_path = filepath.replace(".tar.gz", "-{}".format(collection_files_json))
                        with open(files_path, "wb") as c:
                            c.write(f.read())
        elif type == LoadType.ROLE:
            # get meta/main.yml path
            role_meta_files = safe_glob(
                [
                    os.path.join(filepath, "**", role_meta_main_yml),
                    os.path.join(filepath, "**", role_meta_main_yaml),
                ],
                recursive=True,
            )
            if len(role_meta_files) != 0:
                metafile_path = role_meta_files[0]
        return metafile_path, files_path

    def update_metadata(self, type, metadata_file, target, key, value):
        with open(metadata_file, "r") as f:
            metadata = json.load(f)
        if type == LoadType.COLLECTION:
            metadata_list = metadata.get("collections", [])
        elif type == LoadType.ROLE:
            metadata_list = metadata.get("roles", [])
        else:
            logging.warning("metadata not found: {}".format(target))
            return None
        for i, data in enumerate(metadata_list):
            dm = DownloadMetadata(**data)
            if dm.name == target:
                if hasattr(dm, key):
                    setattr(dm, key, value)
                metadata_list[i] = asdict(dm)
                logging.debug("update {} in metadata: {}".format(key, dm))
                if type == LoadType.COLLECTION:
                    metadata["collections"] = metadata_list
                elif type == LoadType.ROLE:
                    metadata["roles"] = metadata_list
                with open(metadata_file, "w") as f:
                    json.dump(metadata, f)
        return

    def update_role_download_src(self, metadata_file, dst_src_dir):
        with open(metadata_file, "r") as f:
            metadata = json.load(f)
        metadata_list = metadata.get("roles", [])
        for i, data in enumerate(metadata_list):
            dm = DownloadMetadata(**data)
            value = "{}/{}".format(dst_src_dir, dm.name)
            key = "download_src_path"
            if hasattr(dm, key):
                setattr(dm, key, value)
            dm.metafile_path, _ = self.get_metafile_in_target(LoadType.ROLE, value)
            dm.author = self.get_author(LoadType.ROLE, dm.metafile_path)
            metadata_list[i] = asdict(dm)
            logging.debug("update {} in metadata: {}".format(key, dm))
        metadata["roles"] = metadata_list
        with open(metadata_file, "w") as f:
            json.dump(metadata, f)
        return

    def get_author(self, type, metafile_path):
        if not os.path.exists(metafile_path):
            logging.warning("invalid file path: {}".format(metafile_path))
            return ""
        if type == LoadType.COLLECTION:
            with open(metafile_path, "r") as f:
                metadata = json.load(f)
            authors = metadata.get("collection_info", {}).get("authors", [])
            return ",".join(authors)
        elif type == LoadType.ROLE:
            with open(metafile_path, "r") as f:
                metadata = yaml.safe_load(f)
            author = metadata.get("galaxy_info", {}).get("author", "")
            return author


def find_ext_dependencies(path):
    collection_meta_files = safe_glob(os.path.join(path, "**", collection_manifest_json), recursive=True)
    if len(collection_meta_files) > 0:
        collection_path_list = [trim_suffix(f, ["/" + collection_manifest_json]) for f in collection_meta_files]
        collection_path_list = remove_subdirectories(collection_path_list)
        return LoadType.COLLECTION, collection_path_list
    role_meta_files = safe_glob(
        [
            os.path.join(path, "**", role_meta_main_yml),
            os.path.join(path, "**", role_meta_main_yaml),
        ],
        recursive=True,
    )
    if len(role_meta_files) > 0:
        role_path_list = [trim_suffix(f, ["/" + role_meta_main_yml, "/" + role_meta_main_yaml]) for f in role_meta_files]
        role_path_list = remove_subdirectories(role_path_list)
        return LoadType.ROLE, role_path_list
    return LoadType.UNKNOWN, []
