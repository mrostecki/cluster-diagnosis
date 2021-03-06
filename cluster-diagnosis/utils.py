#!/usr/bin/env python
# Copyright 2017 Authors of Cilium
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import namespace

import collections
import sys
import subprocess
import logging
import time

FORMAT = '%(levelname)s %(message)s'
# TODO: Make the logging level configurable.
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format=FORMAT)
if sys.stdout.isatty():
    # running in a real terminal
    # Color code source: http://bit.ly/2zPHiCK
    logging.addLevelName(
        logging.WARNING,
        "\033[1;31m%s\033[1;0m" %
        logging.getLevelName(
            logging.WARNING))
    logging.addLevelName(
        logging.ERROR,
        "\033[1;41m%s\033[1;0m" %
        logging.getLevelName(
            logging.ERROR))
log = logging.getLogger(__name__)

STATUS_RUNNING = 'Running'
STATUS_NOT_RUNNING = 'Not Running'


class ModuleCheck:
    """Checks whether the module conforms to a certain state.

    Args:
        summary (string): A summary of what the check does.
        check_cb (callback): A callback function for performing the check.
    """

    def __init__(
            self,
            summary,
            check_cb):
        self.name = summary
        self.check_cb = check_cb

    def success_cb(self):
        """Default callback function to call when the ModuleCheck succeeds."""
        # TODO: Perform additional actions (like storing debug data in S3)
        log.info("-- Success --\n")
        return

    def failure_cb(self):
        """Default callback function to call when the ModuleCheck fails."""
        # TODO: Perform additional actions (like storing debug data in S3)
        log.error("-- Failure --\n")
        return

    def get_title(self):
        return "-- " + self.name + " --"

    def run(self):
        log.info(self.get_title())
        if not self.check_cb():
            self.failure_cb()
            return False
        else:
            self.success_cb()
            return True


class ModuleCheckGroup:
    """Ordered list of ModuleChecks

    Runs the ModuleChecks in order. If a ModuleCheck fails, the ModuleChecks
     after that ModuleCheck would not be executed.

    Args:
        name (string): the name of the group of ModuleChecks.
        checks (list): the list of ModuleCheck objects.
    """

    def __init__(self, name, checks=None):
        self.name = name
        self.checks = checks

    def get_title(self):
        return "== " + self.name + " =="

    def add(self, check):
        if self.checks is None:
            self.checks = []
        self.checks.append(check)
        return self

    def run(self):
        log.info(self.get_title())
        for check in self.checks:
            if not check.run():
                return False
        return True


ResourceStatus_ = collections.namedtuple(
    'ResourceStatus',
    'namespace name')


# Unlike PodStatus, this class provides an easy-to-extend generic k8s
# resource representation. Feel free to append more resource status.
class ResourceStatus(ResourceStatus_):
    """ A namedtupe with the following elements in this order.
        namespace (string): name of the pod.
        name (string): name of the pod.
    """
    pass


def get_resource_status(type, full_name="", label=""):
    """Returns the ResourceStatus of one particular Kubernetes resource.

    Args:
        type - Kubernetes resource type.
        full_name(optional) - the full name of the Kubernetes resource.
        label(optional) - the attached label of the resource.
    Returns:
        An object of type ResourceStatus.
    Exceptions:
        The goal is to be consistent with get_pod_status.
        If the command execution failed or no resource has been
        found. A RuntimeError exception will be threw.
    """
    cmd = "kubectl get {} --no-headers --all-namespaces " \
          "-o wide --selector \"{}\" " \
          "| grep \"{}\" | awk '{{print $1 \" \" $2}}'"
    cmd = cmd.format(type, label, full_name)
    try:
        encoded_output = subprocess.check_output(cmd, shell=True)
    except subprocess.CalledProcessError as exc:
        log.warning("command to get status of {} has "
                    "failed. error code: "
                    "{} {}".format(full_name,
                                   exc.returncode, exc.output))
        raise RuntimeError(
            "command to get status of {} has "
            "failed. error code: "
            "{} {}".format(full_name,
                           exc.returncode, exc.output))
    output = encoded_output.decode()
    if output == "":
        log.warning(
            "{} \"{}\" with label \"{}\" can't be found in "
            "the cluster".format(type, full_name, label))
        raise RuntimeError(
            "{} {} with label {} can't be found in the cluster".format(
                type, full_name, label))
    # Example line:
    # kube-system cilium
    split_line = output.split(' ')
    return ResourceStatus(namespace=split_line[0],
                          name=split_line[1])


def get_nodes():
    """Returns a list of nodes. """
    COMMAND = "kubectl get nodes | grep -v NAME | awk '{print $1}'"
    try:
        output = subprocess.check_output(COMMAND, shell=True)
    except subprocess.CalledProcessError as grepexc:
        log.error("error code: {} {}".format(grepexc.returncode,
                                             grepexc.output))
        return []
    return output.decode().splitlines()


def get_pod_config(pod_name):
    """Returns the pod config of a k8s pod with name pod_name. """
    COMMAND = "kubectl describe pod {} -n {}".format(pod_name,
                                                     namespace.cilium_ns)
    try:
        encoded_output = subprocess.check_output(COMMAND, shell=True)
    except subprocess.CalledProcessError as grepexc:
        log.error("error code: {} {}".format(grepexc.returncode,
                                             grepexc.output))
        return None
    output = encoded_output.decode()
    if output == "":
        log.error("could not get pod configuration.")
    return output


PodStatus_ = collections.namedtuple('PodStatus',
                                    'name ready_status status node_name '
                                    'namespace')


class PodStatus(PodStatus_):
    """ A namedtupe with the following elements in this order.
        name (string): name of the pod.
        ready_status (string): the ready status of the pod.
        status (string): the status of the pod (e.g. Running).
        node_name (string): the name of the node.
        namespace (string): the namespace of the pod
    """
    pass


def get_pods_summarized_status_iterator(label_selector):
    """Returns a summarized status of the pods by retrieving the status
    multiple times.

    This helps avoid any false negatives that can occur
    in the scenario wherein the status is checked just after a pod restart.

    Args:
        label_selector - the label selector to select the pods.

    Returns:
        An object of type PodStatus.
    """
    pod_status_map = {}
    for attempt in range(0, 5):
        # These retry attempts will take some time. Provide some form of
        # visual feedback to the user.
        # Cannot use log as it'll print on a new line every time.
        sys.stdout.write('.')
        sys.stdout.flush()
        for pod_status in \
                get_pods_status_iterator_by_labels(label_selector, [], False):
            status_verdict = STATUS_RUNNING
            try:
                temp_pod_status = get_pod_status(pod_status.name)
                if (temp_pod_status.status != STATUS_RUNNING):
                    # Prefer not Running status over `Running` status.
                    status_verdict = temp_pod_status.status
            except RuntimeError:
                status_verdict = STATUS_NOT_RUNNING
            pod_status_map[pod_status.name] = PodStatus(
                pod_status.name,
                pod_status.ready_status,
                status_verdict,
                pod_status.node_name,
                pod_status.namespace)
        time.sleep(2)
    sys.stdout.write('\n')
    sys.stdout.flush()
    for pod_name in pod_status_map:
        yield pod_status_map[pod_name]


def get_pod_status(full_pod_name):
    """Returns an iterator to the status of pods.

    Args:
        full_pod_name - the complete pod name.

    Returns:
        An object of type PodStatus.
    """

    cmd = ("kubectl get pods --all-namespaces -o wide "
           "| awk 'BEGIN{{offset=0}}"
           "/NOMINATED/{{offset=1}}"
           "/{}/{{print $2 \" \" $3 \" \" $4 \" \" $(NF-offset) \" \" $1}}'"
           ).format(full_pod_name)
    try:
        encoded_output = subprocess.check_output(cmd, shell=True)
    except subprocess.CalledProcessError as exc:
        log.error("command to get status of {} has "
                  "failed. error code: "
                  "{} {}".format(full_pod_name,
                                 exc.returncode, exc.output))
        return
    output = encoded_output.decode()
    if output == "":
        log.error("pod {} is not running on the cluster".format(
                  full_pod_name))
        raise RuntimeError("pod {} is not running on the cluster".format(
                           full_pod_name))
    # Example line:
    # name-blah-sr64c 0/1 CrashLoopBackOff
    # ip-172-0-33-255.us-west-2.compute.internal kube-system
    split_line = output.split(' ')
    return PodStatus(name=split_line[0],
                     ready_status=split_line[1],
                     status=split_line[2],
                     node_name=split_line[3],
                     namespace=split_line[4])


def get_pods_status_iterator_by_labels(label_selector, host_ip_filter,
                                       must_exist=True):
    """Returns an iterator to the status of pods selected with the
    label selector.

    Args:
        label_selector - the labels used to select the pods.
        e.g. "k8s-app=cilium, kubernetes.io/cluster-service=true"
        must_exist - boolean to indicate that a pod with that name must exist.
            If the condition isn't satisfied, an error will be logged.

    Returns:
        An object of type PodStatus.
    """
    # TODO: Handle the case of a pod w/ multiple containers.
    # Right now, we pick the status of the first container in the pod.
    cmd = ("kubectl get pods --all-namespaces -o wide"
           " --selector={} -o=jsonpath='{{range .items[*]}}"
           "{{@.metadata.name}}{{\" \"}}"
           "{{@.status.containerStatuses[0].ready}}{{\" \"}}"
           "{{@.status.phase}}{{\" \"}}"
           "{{@.spec.nodeName}}{{\" \"}}"
           "{{@.metadata.namespace}}{{\"\\n\"}}'").format(label_selector)
    try:
        encoded_output = subprocess.check_output(cmd, shell=True)
    except subprocess.CalledProcessError as exc:
        log.error("command to get status of {} has "
                  "failed. error code: "
                  "{} {}".format(label_selector,
                                 exc.returncode, exc.output))
        return
    output = encoded_output.decode()
    if output == "":
        if must_exist:
            log.error("no pods with labels "
                      "{} are running on the cluster".format(
                        label_selector))
        return

    # kubectl field selector supports listing pods based on a particular
    # field. However, it doesn't support hostIP field in 1.9.6. Also,
    # it doesn't support set-based filtering. As a result, we will use
    # grep based filtering for now. We might want to switch to this
    # feature in the future. The following filter can be extended by
    # modifying the following kubectl custom-columns and the associated
    # grep command.
    host_ip_filter_cmd = "kubectl get pods --no-headers " \
        "-o=custom-columns=NAME:.metadata.name," \
        "HOSTIP:.status.hostIP --all-namespaces | grep -E \"{}\" | " \
        "awk '{{print $1}}'"
    host_ip_filter_cmd = host_ip_filter_cmd.format(
        "|".join(map(str, host_ip_filter)))
    try:
        filter_output = subprocess.check_output(
            host_ip_filter_cmd, shell=True)
    except subprocess.CalledProcessError as exc:
        log.error("command to list filtered pods has "
                  "failed. error code: "
                  "{} {}".format(exc.returncode, exc.output))
    filter_output = filter_output.decode()
    if filter_output == "":
        if must_exist:
            log.error("No output because all the pods were filtered "
                      "out by the node ip filter {}.".format(
                        host_ip_filter))
        return
    filtered_pod_list = filter_output.splitlines()

    for line in output.splitlines():
        # Example line:
        # name-blah-sr64c 0/1 CrashLoopBackOff
        # ip-172-0-33-255.us-west-2.compute.internal kube-system
        split_line = line.split(' ')
        if split_line[0] not in filtered_pod_list:
            continue
        yield PodStatus(name=split_line[0],
                        ready_status=split_line[1],
                        status=split_line[2],
                        node_name=split_line[3],
                        namespace=split_line[4])


def getopts(argv):
    """Collect command line options in a dictionary.

        We cannot use sys.getopt as it is supported only in Python3.
    """
    opts = {}
    while argv:
        if argv[0][0] == '-':
            if len(argv) > 1:
                opts[argv[0]] = argv[1]
            else:
                opts[argv[0]] = None
        # Reduce the arg list
        argv = argv[1:]
    return opts


def get_current_time():
    return time.strftime("%Y%m%d-%H%M%S")
