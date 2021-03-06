import datetime
import json
import os
import traceback

from dateutil.parser import ParserError, parse

from great_expectations import DataContext
from great_expectations.data_context.store import (
    DatabaseStoreBackend,
    HtmlSiteStore,
    InMemoryStoreBackend,
    MetricStore,
    TupleFilesystemStoreBackend,
    TupleGCSStoreBackend,
    TupleS3StoreBackend,
    ValidationsStore,
)
from great_expectations.data_context.types.resource_identifiers import (
    ValidationResultIdentifier,
)


class UpgradeHelperV11:
    def __init__(self, data_context=None, context_root_dir=None):
        assert (
            data_context or context_root_dir
        ), "Please provide a data_context object or a context_root_dir."

        self.data_context = data_context or DataContext(
            context_root_dir=context_root_dir
        )

        self.upgrade_log = {
            "skipped_validations_stores": {
                "database_store_backends": [],
                "unsupported": [],
            },
            "skipped_docs_validations_stores": {"unsupported": []},
            "skipped_metrics_stores": {
                "database_store_backends": [],
                "unsupported": [],
            },
            "exceptions": [],
            "upgraded_validations_stores": {
                # STORE_NAME: {
                #     "validations_updated": [{
                #         "src": src_url,
                #         "dest": dest_url
                #     }],
                #     "exceptions": BOOL
                # }
            },
            "upgraded_docs_site_validations_stores": {
                # SITE_NAME: {
                #     "validation_result_pages_updated": [{
                #         src: src_url,
                #         dest: dest_url
                #     }],
                #     "exceptions": BOOL
                # }
            },
        }

        self.upgrade_checklist = {
            "validations_store_backends": {},
            "docs_validations_store_backends": {},
        }

        self.validation_run_times = {}

        self.run_time_setters_by_backend_type = {
            TupleFilesystemStoreBackend: self._get_tuple_filesystem_store_backend_run_time,
            TupleS3StoreBackend: self._get_tuple_s3_store_backend_run_time,
            TupleGCSStoreBackend: self._get_tuple_gcs_store_backend_run_time,
        }

        self._generate_upgrade_checklist()

    def _generate_upgrade_checklist(self):
        for (store_name, store) in self.data_context.stores.items():
            if not isinstance(store, (ValidationsStore, MetricStore)):
                continue
            elif isinstance(store, ValidationsStore):
                self._process_validations_store_for_checklist(store_name, store)
            elif isinstance(store, MetricStore):
                self._process_metrics_store_for_checklist(store_name, store)

        sites = (
            self.data_context._project_config_with_variables_substituted.data_docs_sites
        )

        if sites:
            for site_name, site_config in sites.items():
                self._process_docs_site_for_checklist(site_name, site_config)

    def _process_docs_site_for_checklist(self, site_name, site_config):
        site_html_store = HtmlSiteStore(
            store_backend=site_config.get("store_backend"),
            runtime_environment={
                "data_context": self.data_context,
                "root_directory": self.data_context.root_directory,
                "site_name": site_name,
            },
        )
        site_validations_store_backend = site_html_store.store_backends[
            ValidationResultIdentifier
        ]

        if isinstance(
            site_validations_store_backend,
            tuple(list(self.run_time_setters_by_backend_type.keys())),
        ):
            self.upgrade_checklist["docs_validations_store_backends"][
                site_name
            ] = site_validations_store_backend
        else:
            self.upgrade_log["skipped_docs_validations_stores"]["unsupported"].append(
                {
                    "site_name": site_name,
                    "validations_store_backend_class": type(
                        site_validations_store_backend
                    ).__name__,
                }
            )

    def _process_validations_store_for_checklist(self, store_name, store):
        store_backend = store.store_backend
        if isinstance(store_backend, DatabaseStoreBackend):
            self.upgrade_log["skipped_validations_stores"][
                "database_store_backends"
            ].append(
                {
                    "store_name": store_name,
                    "store_backend_class": type(store_backend).__name__,
                }
            )
        elif isinstance(
            store_backend, tuple(list(self.run_time_setters_by_backend_type.keys()))
        ):
            self.upgrade_checklist["validations_store_backends"][
                store_name
            ] = store_backend
        else:
            self.upgrade_log["skipped_validations_stores"]["unsupported"].append(
                {
                    "store_name": store_name,
                    "store_backend_class": type(store_backend).__name__,
                }
            )

    def _process_metrics_store_for_checklist(self, store_name, store):
        store_backend = store.store_backend
        if isinstance(store_backend, DatabaseStoreBackend):
            self.upgrade_log["skipped_metrics_stores"][
                "database_store_backends"
            ].append(
                {
                    "store_name": store_name,
                    "store_backend_class": type(store_backend).__name__,
                }
            )
        elif isinstance(store_backend, InMemoryStoreBackend):
            pass
        else:
            self.upgrade_log["skipped_metrics_stores"]["unsupported"].append(
                {
                    "store_name": store_name,
                    "store_backend_class": type(store_backend).__name__,
                }
            )

    def upgrade_store_backend(self, store_backend, store_name=None, site_name=None):
        assert store_name or site_name, "Must pass either store_name or site_name."
        assert not (
            store_name and site_name
        ), "Must pass either store_name or site_name, not both."

        try:
            validation_source_keys = store_backend.list_keys()
        except Exception as e:
            exception_traceback = traceback.format_exc()
            exception_message = (
                f'{type(e).__name__}: "{str(e)}".  '
                f'Traceback: "{exception_traceback}".'
            )
            self._update_upgrade_log(
                store_backend=store_backend,
                store_name=store_name,
                site_name=site_name,
                exception_message=exception_message,
            )

        for source_key in validation_source_keys:
            try:
                run_name = source_key[-2]
                dest_key = None
                if run_name not in self.validation_run_times:
                    self.run_time_setters_by_backend_type.get(type(store_backend))(
                        source_key, store_backend
                    )
                dest_key_list = list(source_key)
                dest_key_list.insert(-1, self.validation_run_times[run_name])
                dest_key = tuple(dest_key_list)
            except Exception as e:
                exception_traceback = traceback.format_exc()
                exception_message = (
                    f'{type(e).__name__}: "{str(e)}".  '
                    f'Traceback: "{exception_traceback}".'
                )
                self._update_upgrade_log(
                    store_backend=store_backend,
                    source_key=source_key,
                    dest_key=dest_key,
                    store_name=store_name,
                    site_name=site_name,
                    exception_message=exception_message,
                )

            try:
                if store_name:
                    self._update_validation_result_json(
                        source_key=source_key,
                        dest_key=dest_key,
                        run_name=run_name,
                        store_backend=store_backend,
                    )
                else:
                    store_backend.move(source_key, dest_key)
                self._update_upgrade_log(
                    store_backend=store_backend,
                    source_key=source_key,
                    dest_key=dest_key,
                    store_name=store_name,
                    site_name=site_name,
                )
            except Exception as e:
                exception_traceback = traceback.format_exc()
                exception_message = (
                    f'{type(e).__name__}: "{str(e)}".  '
                    f'Traceback: "{exception_traceback}".'
                )
                self._update_upgrade_log(
                    store_backend=store_backend,
                    source_key=source_key,
                    dest_key=dest_key,
                    store_name=store_name,
                    site_name=site_name,
                    exception_message=exception_message,
                )

    def _update_upgrade_log(
        self,
        store_backend,
        source_key=None,
        dest_key=None,
        store_name=None,
        site_name=None,
        exception_message=None,
    ):
        assert store_name or site_name, "Must pass either store_name or site_name."
        assert not (
            store_name and site_name
        ), "Must pass either store_name or site_name, not both."

        try:
            src_url = store_backend.get_url_for_key(source_key) if source_key else "N/A"
        except Exception:
            src_url = f"Unable to generate URL for key: {source_key}"
        try:
            dest_url = store_backend.get_url_for_key(dest_key) if dest_key else "N/A"
        except Exception:
            dest_url = f"Unable to generate URL for key: {dest_key}"

        if not exception_message:
            log_dict = {"src": src_url, "dest": dest_url}
        else:
            key_name = "validation_store_name" if store_name else "site_name"
            log_dict = {
                key_name: store_name if store_name else site_name,
                "src": src_url,
                "dest": dest_url,
                "exception_message": exception_message,
            }
            self.upgrade_log["exceptions"].append(log_dict)

        if store_name:
            if exception_message:
                self.upgrade_log["upgraded_validations_stores"][store_name][
                    "exceptions"
                ] = True
            else:
                self.upgrade_log["upgraded_validations_stores"][store_name][
                    "validations_updated"
                ].append(log_dict)
        else:
            if exception_message:
                self.upgrade_log["upgraded_docs_site_validations_stores"][site_name][
                    "exceptions"
                ] = True
            else:
                self.upgrade_log["upgraded_docs_site_validations_stores"][site_name][
                    "validation_result_pages_updated"
                ].append(log_dict)

    def _update_validation_result_json(
        self, source_key, dest_key, run_name, store_backend
    ):
        new_run_id_dict = {
            "run_name": run_name,
            "run_time": self.validation_run_times[run_name],
        }
        validation_json_dict = json.loads(store_backend.get(source_key))
        validation_json_dict["meta"]["run_id"] = new_run_id_dict
        store_backend.set(dest_key, json.dumps(validation_json_dict))
        store_backend.remove_key(source_key)

    def _get_tuple_filesystem_store_backend_run_time(self, source_key, store_backend):
        run_name = source_key[-2]
        try:
            self.validation_run_times[run_name] = parse(run_name).isoformat()
        except ParserError:
            source_path = os.path.join(
                store_backend.full_base_directory,
                store_backend._convert_key_to_filepath(source_key),
            )
            path_mod_timestamp = os.path.getmtime(source_path)
            path_mod_iso_str = datetime.datetime.fromtimestamp(
                path_mod_timestamp, tz=datetime.timezone.utc
            ).isoformat()
            self.validation_run_times[run_name] = path_mod_iso_str

    def _get_tuple_s3_store_backend_run_time(self, source_key, store_backend):
        import boto3

        s3 = boto3.resource("s3")
        run_name = source_key[-2]

        try:
            self.validation_run_times[run_name] = parse(run_name).isoformat()
        except ParserError:
            source_path = store_backend._convert_key_to_filepath(source_key)
            if not source_path.startswith(store_backend.prefix):
                source_path = os.path.join(store_backend.prefix, source_path)
            source_object = s3.Object(store_backend.bucket, source_path)
            source_object_last_mod = source_object.last_modified.isoformat()

            self.validation_run_times[run_name] = source_object_last_mod

    def _get_tuple_gcs_store_backend_run_time(self, source_key, store_backend):
        from google.cloud import storage

        gcs = storage.Client(project=store_backend.project)
        bucket = gcs.get_bucket(store_backend.bucket)
        run_name = source_key[-2]

        try:
            self.validation_run_times[run_name] = parse(run_name).isoformat()
        except ParserError:
            source_path = store_backend._convert_key_to_filepath(source_key)
            if not source_path.startswith(store_backend.prefix):
                source_path = os.path.join(store_backend.prefix, source_path)
            source_blob_created_time = bucket.get_blob(
                source_path
            ).time_created.isoformat()

            self.validation_run_times[run_name] = source_blob_created_time

    def _get_skipped_store_and_site_names(self):
        validations_stores_with_database_backends = [
            store_dict.get("store_name")
            for store_dict in self.upgrade_log["skipped_validations_stores"][
                "database_store_backends"
            ]
        ]
        metrics_stores_with_database_backends = [
            store_dict.get("store_name")
            for store_dict in self.upgrade_log["skipped_metrics_stores"][
                "database_store_backends"
            ]
        ]

        unsupported_validations_stores = [
            store_dict.get("store_name")
            for store_dict in self.upgrade_log["skipped_validations_stores"][
                "unsupported"
            ]
        ]
        unsupported_metrics_stores = [
            store_dict.get("store_name")
            for store_dict in self.upgrade_log["skipped_metrics_stores"]["unsupported"]
        ]

        stores_with_database_backends = (
            validations_stores_with_database_backends
            + metrics_stores_with_database_backends
        )
        stores_with_unsupported_backends = (
            unsupported_validations_stores + unsupported_metrics_stores
        )
        doc_sites_with_unsupported_backends = [
            doc_site_dict.get("site_name")
            for doc_site_dict in self.upgrade_log["skipped_docs_validations_stores"][
                "unsupported"
            ]
        ]
        return (
            stores_with_database_backends,
            stores_with_unsupported_backends,
            doc_sites_with_unsupported_backends,
        )

    def get_upgrade_prompt(self):
        (
            skip_with_database_backends,
            skip_with_unsupported_backends,
            skip_doc_sites_with_unsupported_backends,
        ) = self._get_skipped_store_and_site_names()
        validations_store_name_checklist = [
            store_name
            for store_name in self.upgrade_checklist[
                "validations_store_backends"
            ].keys()
        ]
        site_name_checklist = [
            site_name
            for site_name in self.upgrade_checklist[
                "docs_validations_store_backends"
            ].keys()
        ]

        upgrade_text = f"""\
**WARNING!**: This automated upgrade helper is currently experimental. Before proceeding, please make sure you have
appropriate backups of your project.

The following Stores and/or Data Docs sites will be upgraded:
    - Validation Stores: {", ".join(validations_store_name_checklist) if validations_store_name_checklist else "None"}
    - Data Docs Sites: {", ".join(site_name_checklist) if site_name_checklist else "None"}

The following Stores and/or Data Docs sites must be upgraded manually, due to having a database backend, or backend
type that is unsupported or unrecognized. Please consult the 0.11.x migration guide for more information:
https://docs.greatexpectations.io/how_to_guides/migrating_versions.html
    - Stores with database backends: {", ".join(skip_with_database_backends) if skip_with_database_backends else "None"}
    - Stores with unsupported/unrecognized backends: {", ".join(skip_with_unsupported_backends) if skip_with_unsupported_backends else "None"}
    - Data Docs sites with unsupported/unrecognized backends: {", ".join(skip_doc_sites_with_unsupported_backends) if skip_doc_sites_with_unsupported_backends else "None"}

Would you like to proceed?
"""
        return upgrade_text

    def upgrade_project(self):
        try:
            for (store_name, store_backend) in self.upgrade_checklist[
                "validations_store_backends"
            ].items():
                self.upgrade_log["upgraded_validations_stores"][store_name] = {
                    "validations_updated": [],
                    "exceptions": False,
                }
                self.upgrade_store_backend(store_backend, store_name=store_name)
        except Exception:
            pass

        try:
            for (site_name, store_backend) in self.upgrade_checklist[
                "docs_validations_store_backends"
            ].items():
                self.upgrade_log["upgraded_docs_site_validations_stores"][site_name] = {
                    "validation_result_pages_updated": [],
                    "exceptions": False,
                }
                self.upgrade_store_backend(store_backend, site_name=site_name)
        except Exception:
            pass

        return self.upgrade_log
