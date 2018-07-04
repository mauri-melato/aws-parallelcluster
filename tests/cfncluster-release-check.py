#!/usr/bin/python
#
# Copyright 2018      Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy
# of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, express or implied. See the License for the specific
# language governing permissions and limitations under the License.
#
#
# Build a cluster for each combination of region, base_os, and
# scheduler, and run a test script on each cluster.  To avoid bouncing
# against limits in each region, the number of simultaneously built
# clusters in each region is a configuration parameter.
#
# NOTE:
# - This script requires python2
# - To simplify this script, at least one subnet in every region
#   to be tested must have a resource tag named "CfnClusterTestSubnet"
#   (value does not matter). That subnet will be used as the launch
#   target for the cluster.

import datetime
import os
import signal
import sys
import subprocess
import threading
import time
import re
import argparse
import Queue
import boto3
import process_helper as prochelp

#
# configuration
#
username_map = { 'alinux' : 'ec2-user',
                 'centos6' : 'centos',
                 'centos7' : 'centos',
                 'ubuntu1404' : 'ubuntu',
                 'ubuntu1604' : 'ubuntu' }

#
# global variables (sigh)
#
setup = {}

results_lock = threading.Lock()
failure = 0
success = 0
aborted = 0

_TIMESTAMP_FORMAT = '%Y%m%d%H%M%S'
_timestamp = datetime.datetime.now().strftime(_TIMESTAMP_FORMAT)



def _dirname():
    return os.path.dirname(os.path.realpath(sys.argv[0]))

def _time():
    return datetime.datetime.now()

#
# run a single test, possibly in parallel
#
def run_test(region, distro, scheduler, instance_type, key_name, key_path):
    testname = '%s-%s-%s-%s-%s' % (region, distro, scheduler, instance_type.replace('.', ''), _timestamp)
    test_filename = "%s-config.cfg" % testname

    sys.stdout.write("--> %s: Starting\n" % (testname))

    file = open(test_filename, "w")
    file.write("[aws]\n")
    file.write("aws_region_name = %s\n" % region)
    file.write("[cluster default]\n")
    file.write("vpc_settings = public\n")
    file.write("key_name = %s\n" % key_name)
    file.write("base_os = %s\n" % distro)
    file.write("master_instance_type = %s\n" % instance_type)
    file.write("compute_instance_type = %s\n" % instance_type)
    file.write("initial_queue_size = 1\n")
    file.write("maintain_initial_size = true\n")
    file.write("scheduler = %s\n" % (scheduler))
    file.write("scaling_settings = custom\n")
    file.write("[vpc public]\n")
    file.write("master_subnet_id = %s\n" % (setup[region]['subnet']))
    file.write("vpc_id = %s\n" % (setup[region]['vpc']))
    file.write("[global]\n")
    file.write("cluster_template = default\n")
    file.write("[scaling custom]\n")
    file.write("scaling_adjustment = 1\n")
    file.write("scaling_period = 30\n")
    file.write("scaling_evaluation_periods = 1\n")
    file.write("scaling_cooldown = 300\n")
    file.close()

    stdout_f = open('%s-stdout.txt' % testname, 'w', 0)
    stderr_f = open('%s-stderr.txt' % testname, 'w', 0)

    master_ip = ''
    username = username_map[distro]
    _create_interrupted = False;
    _create_done = False;
    try:
        # build the cluster
        prochelp.exec_command(['cfncluster', '--config', test_filename, 'create', testname],
                              stdout=stdout_f, stderr=stderr_f)
        _create_done = True
        # get the master ip, which means grepping through cfncluster status gorp
        dump = prochelp.exec_command(['cfncluster', '--config', test_filename,
                                        'status', testname], stderr=stderr_f)
        dump_array = dump.splitlines()
        for line in dump_array:
            m = re.search('MasterPublicIP: (.+)$', line)
            if m:
                master_ip = m.group(1)
                break
        if master_ip == '':
            stderr_f.write('!! %s: Master IP not found; aborting !!\n' % (testname))
            raise Exception('Master IP not found')
        stdout_f.write("--> %s master ip: %s\n" % (testname, master_ip))
        print("--> %s master ip: %s" % (testname, master_ip))

        # run test on the cluster...
        ssh_params = ['-o', 'StrictHostKeyChecking=no']
        ssh_params += ['-o', 'BatchMode=yes']
        # ssh_params += ['-o', 'ConnectionAttempts=30']
        ssh_params += ['-o', 'ConnectTimeout=60']
        ssh_params += ['-o', 'ServerAliveCountMax=5']
        ssh_params += ['-o', 'ServerAliveInterval=30']
        if key_path:
            ssh_params.extend(['-i', key_path])

        prochelp.exec_command(['scp'] + ssh_params + [os.path.join(_dirname(), 'cluster-check.sh'), '%s@%s:.' % (username, master_ip)],
                              stdout=stdout_f, stderr=stderr_f)
        # prochelp.exec_command(
        #     ['ssh', '-n'] + ssh_params + ['%s@%s' % (username, master_ip), '/bin/bash --login cluster-check.sh %s' % scheduler],
        #     stdout=stdout_f, stderr=stderr_f)

        stdout_f.write('SUCCESS:  %s!!\n' % (testname))
        open('%s.success' %testname, 'w').close()
    except prochelp.ProcessHelperError as exc:
        if isinstance(exc, prochelp.KilledProcessError) and not _create_done:
            _create_interrupted = True
            print("[%s][%s]xxx cfncluster create interrupted" % (_time(), testname))
        sys.stdout.write('!! ABORTED: %s!!\n' % (testname))
        stdout_f.write('ABORTED: %s!!\n' % (testname))
        open('%s.aborted' % testname, 'w').close()
        raise exc
    except Exception as exc:
        print("xxx Unexpected exception %s: %s\n" % (str(type(exc)), str((exc))))
        stderr_f.write("Unexpected exception %s: %s" % (str(type(exc)), str((exc))))
        sys.stdout.write("!! FAILURE: %s!!\n" % (testname))
        stdout_f.write('FAILURE: %s!!\n' % (testname))
        open('%s.failed' % testname, 'w').close()
        raise exc
    finally:
        if _create_done:
            _del_iters = 1
        elif _create_interrupted:
            # if the create process was interrupted it may take few seconds for the stack id to be actually registered
            _del_iters = 10
        else:
            # No delete is necessary if the cluster wasn't created
            _del_iters = 0
        if _del_iters > 0:
            _del_done = False
            stdout_f.write('Deleting: %s - iterations: %s\n' % (testname, _del_iters))
            while not _del_done and _del_iters > 0:
                try:
                    sys.stdout.write("--> %s: Deleting - iterations: %s\n" % (testname, _del_iters))
                    print("[%s][%s]xxx Before subprocess.call() for cfncluster delete" % (_time(), testname))
                    # clean up the cluster
#                    subprocess.call(['cfncluster', '--config', test_filename, '-nw', 'delete', testname],
#                                    stdout=stdout_f, stderr=stderr_f)
                    _del_output = subprocess.check_output(['cfncluster', '--config', test_filename, '-nw', 'delete', testname], stderr=stderr_f)
                    _del_done = "DELETE_IN_PROGRESS" in _del_output or "DELETE_COMPLETE" in _del_output
                    stdout_f.write(_del_output + '\n')
                    print("[%s][%s]xxx After subprocess.call() for cfncluster delete - success: %s" % (_time(), testname, _del_done))
                except CalledProcessError:
                    pass
                except (KeyboardInterrupt, Exception) as exc:
                    stderr_f.write("Unexpected exception on launching 'cfncluster delete' %s: %s" % (str(type(exc)), str((exc))))
                finally:
                    if not _del_done and _del_iters > 1:
                        time.sleep(2)
                    _del_iters -= 1

            try:
                print("[%s][%s]xxx Before final cfncluster status" % (_time(), testname))
                # Uusally terminates with exit status 1 since at the end ofthe delete operartion the stack is not found.
                prochelp.exec_command(['cfncluster', '--config', test_filename, 'status', testname], stdout=stdout_f, stderr=stderr_f)
                print("[%s][%s]xxx After final cfncluster status" % (_time(), testname))
            except Exception as exc:
                print("[%s][%s]xxx Expected exception for final cfncluster status" % (_time(), testname))
                pass
        stdout_f.close()
        stderr_f.close()
        # os.remove(test_filename)
    sys.stdout.write("--> %s: Finished\n" % (testname))

#
# worker thread, there will be config['parallelism'] of these running
# per region, dispatching work from the work queue
#
def test_runner(region, q, key_name, key_path):
    global success
    global failure
    global results_lock

    while True:
        item = q.get()

        # just in case we miss an exception in run_test, don't abort everything...
        try:
            run_test(region=region, distro=item['distro'], scheduler=item['scheduler'],
                     instance_type=item['instance_type'], key_name=key_name, key_path=key_path)
            retval = 0
        except Exception as e:
            retval = 1

        results_lock.acquire(True)
        if retval == 0:
            success += 1
        else:
            failure += 1
        results_lock.release()
        q.task_done()

def _bind_term_signals():
    signal.signal(signal.SIGINT, prochelp.term_handler)
    signal.signal(signal.SIGTERM, prochelp.term_handler)
    signal.signal(signal.SIGHUP, prochelp.term_handler)

if __name__ == '__main__':
    _bind_term_signals()

    config = { 'parallelism' : 3,
               'regions' : 'us-east-1,us-east-2,us-west-1,us-west-2,' +
                           'ca-central-1,eu-west-1,eu-west-2,eu-central-1,' +
                           'ap-southeast-1,ap-southeast-2,ap-northeast-1,' +
                           'ap-south-1,sa-east-1,eu-west-3',
               'distros' : 'alinux,centos6,centos7,ubuntu1404,ubuntu1604',
               'schedulers' : 'sge,slurm,torque',
               'instance_types': 'c4.xlarge',
               'key_path' : ''}

    parser = argparse.ArgumentParser(description = 'Test runner for CfnCluster')
    parser.add_argument('--parallelism', help = 'Number of tests per region to run in parallel',
                        type = int, default = 3)
    parser.add_argument('--regions', help = 'Comma separated list of regions to test',
                        type = str)
    parser.add_argument('--distros', help = 'Comma separated list of distributions to test',
                        type = str)
    parser.add_argument('--schedulers', help = 'Comma separated list of schedulers to test',
                        type = str)
    parser.add_argument('--instance-types', type=str,
                        help='Comma separated list of instance types to use for both Master and Compute nodes')
    parser.add_argument('--key-name', help = 'Key Pair to use for EC2 instances',
                        type = str, required = True)
    parser.add_argument('--key-path', help = 'Key path to use for SSH connections',
                        type = str)

    for key, value in vars(parser.parse_args()).iteritems():
        if not value == None:
            config[key] = value

    region_list = config['regions'].split(',')
    distro_list = config['distros'].split(',')
    scheduler_list = config['schedulers'].split(',')
    instance_type_list = config['instance_types'].split(',')

    print("==> Regions: %s" % (', '.join(region_list)))
    print("==> Instance Types: %s" % (', '.join(instance_type_list)))
    print("==> Distros: %s" % (', '.join(distro_list)))
    print("==> Schedulers: %s" % (', '.join(scheduler_list)))
    print("==> Parallelism: %d" % (config['parallelism']))
    print("==> Key Pair: %s" % (config['key_name']))

    if config['key_path']:
        print("==> Key Path: %s" % (config['key_path']))

    # Populate subnet / vpc data for all regions we're going to test.
    for region in region_list:
        client = boto3.client('ec2', region_name=region)
        response = client.describe_tags(Filters=[{'Name': 'key',
                                                  'Values': [ 'CfnClusterTestSubnet' ]}],
                                        MaxResults=16)
        if len(response['Tags']) == 0:
            print('Could not find subnet in %s with CfnClusterTestSubnet tag.  Aborting.' %
                  (region))
            exit(1)
        subnetid = response['Tags'][0]['ResourceId']

        response = client.describe_subnets(SubnetIds = [ subnetid ])
        if len(response) == 0:
            print('Could not find subnet info for %s' % (subnetid))
            exit(1)
        vpcid = response['Subnets'][0]['VpcId']

        setup[region] = { 'vpc' : vpcid, 'subnet' : subnetid }

    work_queues = {}
    # build up a per-region list of work to do
    for region in region_list:
        work_queues[region] = Queue.Queue()
        for distro in distro_list:
            for scheduler in scheduler_list:
                for instance in instance_type_list:
                    work_item = {'distro': distro, 'scheduler': scheduler, 'instance_type': instance}
                    work_queues[region].put(work_item)

    # start all the workers
    for region in region_list:
        for i in range(0, config['parallelism']):
            t = threading.Thread(target=test_runner,
                                 args=(region, work_queues[region], config['key_name'], config['key_path']))
            t.daemon = True
            t.start()


    # WARN: The join() approach prevents the SIGINT signal to be caught from the main thread,
    #       that is actually blocked in the join!
    # wait for all the work queues to be completed in each region
    # for region in region_list:
    #    work_queues[region].join()
    all_finished = False
    while not all_finished:
        time.sleep(1)
        all_finished = True
        for queue in work_queues.values():
            all_finished = all_finished and queue.unfinished_tasks == 0

    print("%s - Empty reqions queues: %s" % (datetime.datetime.now(), all_finished))

    # for region in region_list:
    #     work_queues[region].join()
    # print("%s - End queues join()!" % datetime.datetime.now())

    # print status...
    print("==> Success: %d" % (success))
    print("==> Failure: %d" % (failure))
    if failure != 0:
        exit(1)
