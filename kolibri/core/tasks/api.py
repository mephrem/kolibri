import os

from django.apps.registry import AppRegistryNotReady
from django.core.management import call_command
from django.http.response import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework import viewsets
from rest_framework.decorators import list_route
from rest_framework.response import Response
from six import string_types

from .queue import get_queue
from kolibri.core.content.models import ChannelMetadata
from kolibri.core.content.permissions import CanExportLogs
from kolibri.core.content.permissions import CanManageContent
from kolibri.core.content.utils.channels import get_mounted_drive_by_id
from kolibri.core.content.utils.channels import get_mounted_drives_with_channel_info
from kolibri.core.content.utils.paths import get_content_database_file_path
from kolibri.core.tasks.iceqube.classes import State
from kolibri.core.tasks.iceqube.exceptions import JobNotFound
from kolibri.core.tasks.iceqube.exceptions import UserCancelledError
from kolibri.utils import conf

try:
    from django.apps import apps

    apps.check_apps_ready()
except AppRegistryNotReady:
    import django

    django.setup()


NETWORK_ERROR_STRING = _("There was a network error.")

DISK_IO_ERROR_STRING = _("There was a disk access error.")

CATCHALL_SERVER_ERROR_STRING = _("There was an unknown error.")


def validate_import_export_task(task_description):
    try:
        channel_id = task_description["channel_id"]
    except KeyError:
        raise serializers.ValidationError("The channel_ids field is required.")

    file_size = task_description.get("file_size")

    total_resources = task_description.get("total_resources")

    node_ids = task_description.get("node_ids", None)
    exclude_node_ids = task_description.get("exclude_node_ids", None)

    if node_ids and not isinstance(node_ids, list):
        raise serializers.ValidationError("node_ids must be a list.")

    if exclude_node_ids and not isinstance(exclude_node_ids, list):
        raise serializers.ValidationError("exclude_node_ids must be a list.")

    return {
        "channel_id": channel_id,
        "file_size": file_size,
        "total_resources": total_resources,
        "exclude_node_ids": exclude_node_ids,
        "node_ids": node_ids,
    }


def validate_remote_import_task(task_description):
    import_task = validate_import_export_task(task_description)

    baseurl = task_description.get(
        "baseurl", conf.OPTIONS["Urls"]["CENTRAL_CONTENT_BASE_URL"]
    )

    import_task.update({"baseurl": baseurl})

    return import_task


def validate_local_import_export_task(task_description):
    import_task = validate_import_export_task(task_description)

    try:
        drive_id = task_description["drive_id"]
    except KeyError:
        raise serializers.ValidationError("The drive_id field is required.")

    try:
        drive = get_mounted_drive_by_id(drive_id)
    except KeyError:
        raise serializers.ValidationError(
            "That drive_id was not found in the list of drives."
        )

    import_task.update({"drive_id": drive_id, "datafolder": drive.datafolder})

    return import_task


class TasksViewSet(viewsets.ViewSet):
    def get_permissions(self):
        # task permissions shared between facility management and device management
        if self.action in ["list", "deletefinishedtasks"]:
            permission_classes = [CanManageContent | CanExportLogs]
        # exclusive permission for facility management
        elif self.action == "startexportlogcsv":
            permission_classes = [CanExportLogs]
        # this was the default before, so leave as is for any other endpoints
        else:
            permission_classes = [CanManageContent]
        return [permission() for permission in permission_classes]

    def list(self, request):
        jobs_response = [_job_to_response(j) for j in get_queue().jobs]

        return Response(jobs_response)

    def create(self, request):
        # unimplemented. Call out to the task-specific APIs for now.
        pass

    def retrieve(self, request, pk=None):
        try:
            task = _job_to_response(get_queue().fetch_job(pk))
            return Response(task)
        except JobNotFound:
            raise Http404("Task with {pk} not found".format(pk=pk))

    def destroy(self, request, pk=None):
        # unimplemented for now.
        pass

    @list_route(methods=["post"])
    def startremotebulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(validate_remote_import_task, request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "REMOTEIMPORT", "started_by": request.user.pk})
            import_job_id = get_queue().enqueue(
                _remoteimport,
                task["channel_id"],
                task["baseurl"],
                extra_metadata=task,
                cancellable=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(get_queue().fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startremotechannelimport(self, request):

        task = validate_remote_import_task(request.data)

        task.update({"type": "REMOTECHANNELIMPORT", "started_by": request.user.pk})

        job_id = get_queue().enqueue(
            call_command,
            "importchannel",
            "network",
            task["channel_id"],
            baseurl=task["baseurl"],
            extra_metadata=task,
            cancellable=True,
        )
        resp = _job_to_response(get_queue().fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startremotecontentimport(self, request):

        task = validate_remote_import_task(request.data)

        task.update({"type": "REMOTECONTENTIMPORT", "started_by": request.user.pk})

        job_id = get_queue().enqueue(
            call_command,
            "importcontent",
            "network",
            task["channel_id"],
            baseurl=task["baseurl"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
            track_progress=True,
            cancellable=True,
        )

        resp = _job_to_response(get_queue().fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskbulkimport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(validate_local_import_export_task, request.data)

        job_ids = []

        for task in tasks:
            task.update({"type": "DISKIMPORT", "started_by": request.user.pk})
            import_job_id = get_queue().enqueue(
                _diskimport,
                task["channel_id"],
                task["datafolder"],
                extra_metadata=task,
                cancellable=True,
            )
            job_ids.append(import_job_id)

        resp = [_job_to_response(get_queue().fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskchannelimport(self, request):
        task = validate_local_import_export_task(request.data)

        task.update({"type": "DISKCHANNELIMPORT", "started_by": request.user.pk})

        job_id = get_queue().enqueue(
            call_command,
            "importchannel",
            "disk",
            task["channel_id"],
            task["datafolder"],
            extra_metadata=task,
            cancellable=True,
        )

        resp = _job_to_response(get_queue().fetch_job(job_id))
        return Response(resp)

    @list_route(methods=["post"])
    def startdiskcontentimport(self, request):
        task = validate_local_import_export_task(request.data)

        task.update({"type": "DISKCONTENTIMPORT", "started_by": request.user.pk})

        job_id = get_queue().enqueue(
            call_command,
            "importcontent",
            "disk",
            task["channel_id"],
            task["datafolder"],
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
            track_progress=True,
            cancellable=True,
        )

        resp = _job_to_response(get_queue().fetch_job(job_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startbulkdelete(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        for task in request.data:
            try:
                task["channel_id"]
            except KeyError:
                raise serializers.ValidationError("The channel_id field is required.")

        job_ids = []

        for task in request.data:
            try:
                channel = ChannelMetadata.objects.get(id=task["channel_id"])
                job_metadata = {
                    "type": "DELETECHANNEL",
                    "started_by": request.user.pk,
                    "file_size": channel.published_size,
                    "resource_count": channel.total_resource_count,
                }
                delete_job_id = get_queue().enqueue(
                    call_command,
                    "deletechannel",
                    task["channel_id"],
                    track_progress=True,
                    extra_metadata=job_metadata,
                )
                job_ids.append(delete_job_id)
            except ChannelMetadata.DoesNotExist:
                continue

        resp = [_job_to_response(get_queue().fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdeletechannel(self, request):
        """
        Delete a channel and all its associated content from the server
        """

        if "channel_id" not in request.data:
            raise serializers.ValidationError("The 'channel_id' field is required.")

        channel_id = request.data["channel_id"]

        job_metadata = {
            "type": "DELETECHANNEL",
            "started_by": request.user.pk,
            "channel_id": channel_id,
        }

        task_id = get_queue().enqueue(
            call_command,
            "deletechannel",
            channel_id,
            track_progress=True,
            extra_metadata=job_metadata,
        )

        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(get_queue().fetch_job(task_id))

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskbulkexport(self, request):
        if not isinstance(request.data, list):
            raise serializers.ValidationError(
                "POST data must be a list of task descriptions"
            )

        tasks = map(validate_local_import_export_task, request.data)

        job_ids = []

        for task in tasks:
            try:
                channel = ChannelMetadata.objects.get(id=task["channel_id"])
                job_metadata = {
                    "type": "DISKEXPORT",
                    "started_by": request.user.pk,
                    "file_size": channel.published_size,
                    "resource_count": channel.total_resource_count,
                }
                export_job_id = get_queue().enqueue(
                    _localexport,
                    task["channel_id"],
                    task["drive_id"],
                    track_progress=True,
                    cancellable=True,
                    extra_metadata=job_metadata,
                )
                job_ids.append(export_job_id)
            except ChannelMetadata.DoesNotExist:
                continue

        resp = [_job_to_response(get_queue().fetch_job(job_id)) for job_id in job_ids]

        return Response(resp)

    @list_route(methods=["post"])
    def startdiskexport(self, request):
        """
        Export a channel to a local drive, and copy content to the drive.

        """

        task = validate_local_import_export_task(request.data)

        channel = ChannelMetadata.objects.get(id=task["channel_id"])

        task.update(
            {
                "type": "DISKEXPORT",
                "started_by": request.user.pk,
                "file_size": channel.published_size,
                "resource_count": channel.total_resource_count,
            }
        )

        task_id = get_queue().enqueue(
            _localexport,
            task["channel_id"],
            task["drive_id"],
            track_progress=True,
            cancellable=True,
            node_ids=task["node_ids"],
            exclude_node_ids=task["exclude_node_ids"],
            extra_metadata=task,
        )

        # attempt to get the created Task, otherwise return pending status
        resp = _job_to_response(get_queue().fetch_job(task_id))

        return Response(resp)

    @list_route(methods=["post"])
    def canceltask(self, request):
        """
        Cancel a task with its task id given in the task_id parameter.
        """

        if "task_id" not in request.data:
            raise serializers.ValidationError("The 'task_id' field is required.")
        if not isinstance(request.data["task_id"], string_types):
            raise serializers.ValidationError("The 'task_id' should be a string.")
        try:
            get_queue().cancel(request.data["task_id"])
        except JobNotFound:
            pass

        return Response({})

    @list_route(methods=["post"])
    def cleartasks(self, request):
        """
        Cancels all running tasks.
        """

        get_queue().empty()
        return Response({})

    @list_route(methods=["post"])
    def deletefinishedtasks(self, request):
        """
        Delete all tasks that have succeeded, failed, or been cancelled.
        """
        task_id = request.data.get("task_id")
        if task_id:
            get_queue().clear_job(task_id)
        else:
            get_queue().clear()
        return Response({})

    @list_route(methods=["get"])
    def localdrive(self, request):
        drives = get_mounted_drives_with_channel_info()

        # make sure everything is a dict, before converting to JSON
        assert isinstance(drives, dict)
        out = [mountdata._asdict() for mountdata in drives.values()]

        return Response(out)

    @list_route(methods=["post"])
    def startexportlogcsv(self, request):
        """
        Dumps in csv format the required logs.
        By default it will be dump contentsummarylog.

        :param: logtype: Kind of log to dump, summary or session
        :returns: An object with the job information

        """
        csv_export_filenames = {
            "session": "content_session_logs.csv",
            "summary": "content_summary_logs.csv",
        }
        log_type = request.data.get("logtype", "summary")
        if log_type in csv_export_filenames.keys():
            logs_dir = os.path.join(conf.KOLIBRI_HOME, "log_export")
            filepath = os.path.join(logs_dir, csv_export_filenames[log_type])
        else:
            raise Http404(
                "Impossible to create a csv export file for {}".format(log_type)
            )
        if not os.path.isdir(logs_dir):
            os.mkdir(logs_dir)

        job_type = (
            "EXPORTSUMMARYLOGCSV" if log_type == "summary" else "EXPORTSESSIONLOGCSV"
        )

        job_metadata = {"type": job_type, "started_by": request.user.pk}

        job_id = get_queue().enqueue(
            call_command,
            "exportlogs",
            log_type=log_type,
            output_file=filepath,
            overwrite="true",
            extra_metadata=job_metadata,
            track_progress=True,
        )

        resp = _job_to_response(get_queue().fetch_job(job_id))

        return Response(resp)


def _remoteimport(
    channel_id,
    baseurl,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):

    call_command(
        "importchannel",
        "network",
        channel_id,
        baseurl=baseurl,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    call_command(
        "importcontent",
        "network",
        channel_id,
        baseurl=baseurl,
        node_ids=node_ids,
        exclude_node_ids=exclude_node_ids,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )


def _diskimport(
    channel_id,
    drive_id,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):

    call_command(
        "importchannel",
        "network",
        channel_id,
        drive_id,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    call_command(
        "importcontent",
        "network",
        channel_id,
        drive_id,
        node_ids=node_ids,
        exclude_node_ids=exclude_node_ids,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )


def _localexport(
    channel_id,
    drive_id,
    update_progress=None,
    check_for_cancel=None,
    node_ids=None,
    exclude_node_ids=None,
    extra_metadata=None,
):
    drive = get_mounted_drive_by_id(drive_id)

    call_command(
        "exportchannel",
        channel_id,
        drive.datafolder,
        update_progress=update_progress,
        check_for_cancel=check_for_cancel,
    )
    try:
        call_command(
            "exportcontent",
            channel_id,
            drive.datafolder,
            node_ids=node_ids,
            exclude_node_ids=exclude_node_ids,
            update_progress=update_progress,
            check_for_cancel=check_for_cancel,
        )
    except UserCancelledError:
        try:
            os.remove(
                get_content_database_file_path(channel_id, datafolder=drive.datafolder)
            )
        except OSError:
            pass
        raise


def _job_to_response(job):
    if not job:
        return {
            "type": None,
            "started_by": None,
            "status": State.SCHEDULED,
            "percentage": 0,
            "progress": [],
            "id": None,
            "cancellable": False,
        }
    else:
        output = {
            "status": job.state,
            "exception": str(job.exception),
            "traceback": str(job.traceback),
            "percentage": job.percentage_progress,
            "id": job.job_id,
            "cancellable": job.cancellable,
        }
        output.update(job.extra_metadata)
        return output
