#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import json
import os
from pyzip import PyZip
from mldata.config import now, CACHE_TIME, segments
from mldata.element import Element
from mldata.wrapper.api_wrapper import APIWrapper
from mldata.interpreters.interpreter import Interpreter
from mldata.wrapper.smart_updater import AsyncSmartUpdater

__author__ = 'Iván de Paz Centeno'

pool_keys = ThreadPoolExecutor(4)
pool_content = ThreadPoolExecutor(4)


class Dataset(APIWrapper):
    def __init__(self, url_prefix: str, title: str, description: str, reference: str, tags: list, token: str=None,
                 binary_interpreter: Interpreter=None, token_info: dict=None, server_info: dict=None,
                 use_smart_updater: AsyncSmartUpdater=True):
        self.data = {}

        self.binary_interpreter = binary_interpreter
        """:type : Interpreter"""

        if "/" in url_prefix:
            self.data['url_prefix'] = url_prefix
        else:
            if token is not None:
                token_prefix = token.get_prefix()
                self.data['url_prefix'] = '{}/{}'.format(token_prefix, url_prefix)

        self.data['title'] = title
        self.data['description'] = description
        self.data['tags'] = tags
        self.data['reference'] = reference
        self.elements_count = 0
        self.comments_count = 0
        self.page_cache = {}
        self.last_cache_time = now()
        super().__init__(token, token_info=token_info, server_info=server_info)

        # Server_info is only available after super() init.
        if use_smart_updater:
            self.smart_updater = AsyncSmartUpdater(self.server_info, self)
        else:
            self.smart_updater = None

    def __repr__(self):
        return "Dataset {} ({} elements); tags: {}; description: {}; reference: {}".format(self.get_title(), len(self),
                                                                                           self.get_tags(),
                                                                                           self.get_description(),
                                                                                           self.get_reference())

    def set_binary_interpreter(self, binary_interpreter):
        self.binary_interpreter = binary_interpreter

    def get_url_prefix(self):
        return self.data['url_prefix']

    def get_description(self):
        return self.data['description']

    def get_title(self):
        return self.data['title']

    def get_tags(self):
        return self.data['tags']

    def get_reference(self):
        return self.data['reference']

    def set_description(self, new_desc):
        self.data['description'] = new_desc

    def set_title(self, new_title):
        self.data['title'] = new_title

    def set_tags(self, new_tags):
        self.data['tags'] = new_tags

    def set_reference(self, new_reference):
        self.data['reference'] = new_reference

    def update(self):
        self._patch_json("/datasets/{}".format(self.get_url_prefix()),
                         json_data={k: v for k, v in self.data.items() if k != "url_prefix"})

    @classmethod
    def from_dict(cls, definition, token, binary_interpreter=None, token_info=None, server_info=None):

        dataset = cls(definition['url_prefix'], definition['title'], definition['description'], definition['reference'],
                      definition['tags'], token=token, binary_interpreter=binary_interpreter, token_info=token_info,
                      server_info=server_info)

        dataset.comments_count = definition['comments_count']
        dataset.elements_count = definition['elements_count']
        return dataset

    def add_element(self, title: str, description: str, tags: list, http_ref: str, content, interpret=True) -> Element:

        if type(content) is str:
            # content is a URI
            if not os.path.exists(content):
                raise Exception("content must be a binary data or a URI to a file.")

            with open(content, "rb") as f:
                content_bytes = f.read()

            if self.binary_interpreter is None:
                content = content_bytes
            else:
                content = self.binary_interpreter.cipher(content_bytes)

        result = self._post_json("datasets/{}/elements".format(self.get_url_prefix()), json_data={
            'title': title,
            'description': description,
            'tags': tags,
            'http_ref': http_ref,
        })

        self.refresh()

        element = self[result]
        element.set_content(content, interpret)
        return element

    def _request_segment(self, ids):
        results = self._get_json("datasets/{}/elements/bundle".format(self.get_url_prefix()),
                                 json_data={'elements': ids})

        # Warning: what if 'result' does not have the elements ordered in the same way as 'ids'?
        # Todo: reorder 'elements' to match the order of 'ids'
        elements = [
            Element.from_dict(result, self, self.token, self.binary_interpreter, token_info=self.token_info,
                              server_info=self.server_info, smart_updater=self.smart_updater)
            for result in results
            ]

        future = pool_content.submit(self._retrieve_segment_contents, ids)

        for element in elements:
            element.content_promise = future

        return elements

    def _retrieve_segment_contents(self, ids):
        packet_bytes = self._get_binary("datasets/{}/elements/content".format(self.get_url_prefix()),
                                        json_data={'elements': ids})
        packet = PyZip.from_bytes(packet_bytes)

        return dict(packet)

    def __getitem__(self, key):
        if type(key) is not slice and len(str(key)) < 8:
            key = int(key)
            key = slice(key, key + 1, 1)

        elements = []

        if type(key) is slice:
            step = key.step
            start = key.start
            stop = key.stop
            if key.step is None: step = 1
            if key.stop is None: stop = len(self)
            if key.stop < 0: stop = len(self) - stop

            ps = self.server_info['Page-Size']

            ids = [self._get_key(i) for i in range(start, stop, step)]

            futures = [pool_keys.submit(self._request_segment, ids) for segment in segments(ids, ps)]

            elements = []

            for future in futures:
                elements += future.result()

        elif type(key) is str:
            try:
                response = self._get_json("datasets/{}/elements/{}".format(self.get_url_prefix(), key))
                element = Element.from_dict(response, self, self.token, self.binary_interpreter,
                                            token_info=self.token_info, server_info=self.server_info,
                                            smart_updater=self.smart_updater)
                element.content_promise = pool_content.submit(element._retrieve_content)
                elements = [element]

            except Exception as ex:
                elements = []

        else:
            raise KeyError("Type of key not allowed.")

        if len(elements) > 1:
            result = elements
        elif len(elements) == 1:
            result = elements[0]
        else:
            raise KeyError("{} not found".format(key))

        return result

    def __delitem__(self, key):
        result = self._delete_json("datasets/{}/elements/{}".format(self.get_url_prefix(), key))
        self.refresh()

    def _get_elements(self, page):
        return [Element.from_dict(element, self, self.token, self.binary_interpreter, token_info=self.token_info,
                                  server_info=self.server_info, smart_updater=self.smart_updater) for element in
                self._get_json("datasets/{}/elements".format(self.get_url_prefix()), extra_data={'page': page})]

    def __iter__(self):
        """
        :rtype: Element
        :return:
        """
        ps = self.server_info['Page-Size']
        number_of_pages = len(self) // ps + int(len(self) % ps > 0)

        buffer = None

        for page in range(number_of_pages):

            if buffer is None:
                buffer = pool_keys.submit(self._get_elements, page)

            buffer2 = pool_keys.submit(self._get_elements, page + 1)

            elements = buffer.result()

            future = pool_content.submit(self._retrieve_segment_contents, [element.get_id() for element in elements])

            for element in elements:
                element.content_promise = future
                yield element

            buffer = buffer2

    def _get_key(self, key_index):
        ps = int(self.server_info['Page-Size'])
        key_page = key_index // ps
        index = key_index % ps

        try:
            if (now() - self.last_cache_time).total_seconds() > CACHE_TIME:
                self.page_cache.clear()

            page = self.page_cache[key_page]

        except KeyError as ex:
            # Cache miss
            page = self._get_json("datasets/{}/elements".format(self.get_url_prefix()), extra_data={'page': key_page})
            self.page_cache[key_page] = page
            self.last_cache_time = now()

        return page[index]['_id']

    def keys(self, page=-1):
        if page == -1:
            data = [self._get_key(i) for i in range(len(self))]
        else:
            data = [element['_id'] for element in
                    self._get_json("datasets/{}/elements".format(self.get_url_prefix()), extra_data={'page': page})]

        return data

    def __len__(self):
        return self.elements_count

    def __str__(self):
        data = dict(self.data)
        data['num_elements'] = len(self)
        return str(data)

    def save_to_folder(self, folder, metadata_format="json", elements_extension=None, use_numbered_ids=False,
                       only_metadata=False):
        try:
            os.mkdir(folder)
        except Exception as ex:
            pass

        format_saver = {
            "csv": self.__save_csv,
            "json": self.__save_json
        }

        if elements_extension is not None and elements_extension.startswith("."):
            elements_extension = elements_extension[1:]

        if metadata_format not in format_saver:
            raise Exception("format {} for metadata not supported.".format(metadata_format))

        print("Collecting metadata...")
        metadata = {}
        id = -1

        for element in self:

            if use_numbered_ids:
                id += 1
            else:
                id = element.get_id()

            if elements_extension is None:
                element_id = id
            else:
                element_id = "{}.{}".format(id, elements_extension)

            metadata[element_id] = {
                'id': element.get_id(),
                'title': element.get_title(),
                'description': element.get_description(),
                'http_ref': element.get_ref(),
                'tags': element.get_tags(),
            }

        format_saver[metadata_format](folder, metadata)
        print("Saved metadata in format {}".format(metadata_format))

        if only_metadata:
            return

        content_folder = os.path.join(folder, "content")
        try:
            os.mkdir(content_folder)
        except FileExistsError as ex:
            pass

        print("Fetching elements...")

        count = len(metadata)
        it = -1
        for filename, values in metadata.items():
            it += 1
            id = values['id']
            binary_content = self[id].get_content(interpret=False)
            with open(os.path.join(content_folder, filename), "wb") as f:
                f.write(binary_content)

            print("\rProgress: {}%".format(round(it / (count + 0.0001) * 100, 2)), end="", flush=True)

        print("\rProgress: 100%", end="", flush=True)
        print("\nFinished")

    def __save_json(self, folder, metadata):
        with open(os.path.join(folder, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=4)

        with open(os.path.join(folder, "dataset_info.json"), "w") as f:
            data = dict(self.data)
            data['num_elements'] = len(self)
            data['tags'] = data['tags']
            json.dump(data, f, indent=4)

    def __save_csv(self, folder, metadata):
        with open(os.path.join(folder, "metadata.csv"), 'w', newline="") as f:
            writer = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            headers = ['file_name', 'id', 'title', 'description', 'http_ref', 'tags']
            writer.writerow(headers)

            for k, v in metadata.items():
                v['tags'] = "'{}'".format(";".join(v['tags']))
                writer.writerow([k] + [v[h] for h in headers if h != 'file_name'])

        with open(os.path.join(folder, "dataset_info.csv"), "w", newline="") as f:
            writer = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            data = dict(self.data)
            data['num_elements'] = len(self)
            data['tags'] = "'{}'".format(";".join(data['tags']))
            headers = ['url_prefix', 'title', 'description', 'num_elements', 'reference', 'tags']
            writer.writerow(headers)
            writer.writerow([data[h] for h in headers])

    def refresh(self):
        dataset_data = self._get_json("datasets/{}".format(self.get_url_prefix()))
        self.elements_count = dataset_data['elements_count']
        self.comments_count = dataset_data['comments_count']
        self.data = {k: dataset_data[k] for k in ['url_prefix', 'title', 'description', 'reference', 'tags']}

    def close(self):
        if self.smart_updater is not None:
            self.smart_updater.stop()
