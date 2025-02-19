""" Aviatrix Controller HA Lambda script """

import time
import os
import uuid
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPHandler, Request
import traceback
import urllib3
from urllib3.exceptions import InsecureRequestWarning
import requests
import boto3
import botocore
import version
urllib3.disable_warnings(InsecureRequestWarning)

MAX_LOGIN_TIMEOUT = 800
WAIT_DELAY = 30

INITIAL_SETUP_WAIT = 180
INITIAL_SETUP_DELAY = 10

INITIAL_SETUP_API_WAIT = 20
AMI_ID = 'https://aviatrix-download.s3-us-west-2.amazonaws.com/AMI_ID/ami_id.json'
MAXIMUM_BACKUP_AGE = 24 * 3600 * 3   # 3 days


class AvxError(Exception):
    """ Error class for Aviatrix exceptions"""


print('Loading function')


def lambda_handler(event, context):
    """ Entry point of the lambda script"""
    try:
        _lambda_handler(event, context)
    except AvxError as err:
        print('Operation failed due to: ' + str(err))
    except Exception as err:    # pylint: disable=broad-except
        print(str(traceback.format_exc()))
        print("Lambda function failed due to " + str(err))


def _lambda_handler(event, context):
    """ Entry point of the lambda script without exception hadling
        This lambda function will serve 2 kinds of requests:
        one time request from CFT - Request to setup HA (setup_ha method)
         made by Cloud formation template.
        sns_event - Request from sns to attach elastic ip to new instance
         created after controller failover. """
    # scheduled_event = False
    sns_event = False
    print("Version: %s Event: %s" % (version.VERSION, event))
    try:
        cf_request = event["StackId"]
        print("From CFT")
    except (KeyError, AttributeError, TypeError):
        cf_request = None
        print("Not from CFT")
    try:
        sns_event = event["Records"][0]["EventSource"] == "aws:sns"
        print("From SNS Event")
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    if os.environ.get("TESTPY") == "True":
        print("Testing")
        client = boto3.client(
            'ec2', region_name=os.environ["AWS_TEST_REGION"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_BACK"],
            aws_secret_access_key=os.environ["AWS_SECRET_KEY_BACK"])
        lambda_client = boto3.client(
            'lambda', region_name=os.environ["AWS_TEST_REGION"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_BACK"],
            aws_secret_access_key=os.environ["AWS_SECRET_KEY_BACK"])
    else:
        client = boto3.client('ec2')
        lambda_client = boto3.client('lambda')

    tmp_sg = os.environ.get('TMP_SG_GRP', '')
    if tmp_sg:
        print("Lambda probably did not complete last time. Reverting sg %s" % tmp_sg)
        update_env_dict(lambda_client, context, {'TMP_SG_GRP': ''})
        restore_security_group_access(client, tmp_sg)
    try:
        instance_name = os.environ.get('AVIATRIX_TAG')
        controller_instanceobj = client.describe_instances(
            Filters=[
                {'Name': 'instance-state-name', 'Values': ['running']},
                {'Name': 'tag:Name', 'Values': [instance_name]}]
        )['Reservations'][0]['Instances'][0]
    except Exception as err:
        err_reason = "Can't find Controller instance with name tag %s. %s" % (instance_name,
                                                                              str(err))
        print(err_reason)
        if cf_request:
            print("From CF Request")
            if event.get("RequestType", None) == 'Create':
                print("Create Event")
                send_response(event, context, 'FAILED', err_reason)
                return
            print("Ignoring delete CFT for no Controller")
            # While deleting cloud formation template, this lambda function
            # will be called to delete AssignEIP resource. If the controller
            # instance is not present, then cloud formation will be stuck
            # in deletion.So just pass in that case.
            send_response(event, context, 'SUCCESS', '')
            return

        try:
            sns_msg_event = (json.loads(event["Records"][0]["Sns"]["Message"]))['Event']
            print(sns_msg_event)
        except (KeyError, IndexError, ValueError) as err:
            raise AvxError("1.Could not parse SNS message %s" % str(err)) from err
        if not sns_msg_event == "autoscaling:EC2_INSTANCE_LAUNCH_ERROR":
            print("Not from launch error. Exiting")
            return
        print("From the instance launch error. Will attempt to re-create Auto scaling group")

    if cf_request:
        try:
            response_status, err_reason = handle_cloud_formation_request(
                client, event, lambda_client, controller_instanceobj, context, instance_name)
        except AvxError as err:
            err_reason = str(err)
            print(err_reason)
            response_status = 'FAILED'
        except Exception as err:       # pylint: disable=broad-except
            err_reason = str(err)
            print(traceback.format_exc())
            response_status = 'FAILED'

        # Send response to CFT.
        if response_status not in ['SUCCESS', 'FAILED']:
            response_status = 'FAILED'
        send_response(event, context, response_status, err_reason)
        print("Sent {} to CFT.".format(response_status))
    elif sns_event:
        try:
            sns_msg_json = json.loads(event["Records"][0]["Sns"]["Message"])
            sns_msg_event = sns_msg_json['Event']
            sns_msg_desc = sns_msg_json.get('Description', "")
        except (KeyError, IndexError, ValueError) as err:
            raise AvxError("2. Could not parse SNS message %s" % str(err)) from err
        print("SNS Event %s Description %s " % (sns_msg_event, sns_msg_desc))
        if sns_msg_event == "autoscaling:EC2_INSTANCE_LAUNCH":
            print("Instance launched from Autoscaling")
            handle_ha_event(client, lambda_client, controller_instanceobj, context)
        elif sns_msg_event == "autoscaling:TEST_NOTIFICATION":
            print("Successfully received Test Event from ASG")
        elif sns_msg_event == "autoscaling:EC2_INSTANCE_LAUNCH_ERROR":
            # and "The security group" in sns_msg_desc and "does not exist in VPC" in sns_msg_desc:
            print("Instance launch error, recreating with new security group configuration")
            sg_id = create_new_sg(client)
            ami_id = os.environ.get('AMI_ID')
            inst_type = os.environ.get('INST_TYPE')
            key_name = os.environ.get('KEY_NAME')
            delete_resources(None, detach_instances=False)
            setup_ha(ami_id, inst_type, None, key_name, [sg_id], context, attach_instance=False)
    else:
        print("Unknown source. Not from CFT or SNS")


def handle_cloud_formation_request(client, event, lambda_client, controller_instanceobj, context,
                                   instance_name):
    """Handle Requests from cloud formation"""
    response_status = 'SUCCESS'
    err_reason = ''
    if event['RequestType'] == 'Create':
        try:
            os.environ['TOPIC_ARN'] = 'N/A'
            os.environ['S3_BUCKET_REGION'] = ""
            set_environ(client, lambda_client, controller_instanceobj, context)
            print("Environment variables have been set.")
        except Exception as err:
            err_reason = "Failed to setup environment variables %s" % str(err)
            print(err_reason)
            return 'FAILED', err_reason

        if not verify_iam(controller_instanceobj):
            return 'FAILED', 'IAM role aviatrix-role-ec2 could not be verified to be attached to' \
                             ' controller'
        bucket_status, bucket_region = verify_bucket(controller_instanceobj)
        os.environ['S3_BUCKET_REGION'] = bucket_region
        update_env_dict(lambda_client, context, {"S3_BUCKET_REGION": bucket_region})
        if not bucket_status:
            return 'FAILED', 'Unable to verify S3 bucket'
        backup_file_status, backup_file = verify_backup_file(controller_instanceobj)
        if not backup_file_status:
            return 'FAILED', 'Cannot find backup file in the bucket'
        if not is_backup_file_is_recent(backup_file):
            return 'FAILED', f'Backup file is older than {MAXIMUM_BACKUP_AGE}'
        if not assign_eip(client, controller_instanceobj, None):
            return 'FAILED', 'Failed to associate EIP or EIP was not found.' \
                             ' Please attach an EIP to the controller before enabling HA'
        if not _check_ami_id(controller_instanceobj['ImageId']):
            return 'FAILED', "AMI is not latest. Cannot enable Controller HA. Please backup" \
                             "/restore to the latest AMI before enabling controller HA"

        print("Verified AWS and controller Credentials and backup file, EIP and AMI ID")
        print("Trying to setup HA")
        try:
            ami_id = controller_instanceobj['ImageId']
            inst_id = controller_instanceobj['InstanceId']
            inst_type = controller_instanceobj['InstanceType']
            key_name = controller_instanceobj.get('KeyName', '')
            sgs = [sg_['GroupId'] for sg_ in controller_instanceobj['SecurityGroups']]
            setup_ha(ami_id, inst_type, inst_id, key_name, sgs, context)
        except Exception as err:
            response_status = 'FAILED'
            err_reason = "Failed to setup HA. %s" % str(err)
            print(err_reason)
    elif event['RequestType'] == 'Delete':
        try:
            print("Trying to delete lambda created resources")
            inst_id = controller_instanceobj['InstanceId']
            delete_resources(inst_id)
        except Exception as err:
            err_reason = "Failed to delete lambda created resources. %s" % str(err)
            print(err_reason)
            print("You'll have to manually delete Auto Scaling group,"
                  " Launch Configuration, and SNS topic, all with"
                  " name {}.".format(instance_name))
            response_status = 'FAILED'
    return response_status, err_reason


def _check_ami_id(ami_id):
    """ Check if AMI is latest"""
    print("Verifying AMI ID")
    resp = requests.get(AMI_ID)
    ami_dict = json.loads(resp.content)
    for image_type in ami_dict:
        if ami_id in list(ami_dict[image_type].values()):
            print("AMI is valid")
            return True
    print("AMI is not latest. Cannot enable Controller HA. Please backup restore to the latest AMI"
          "before enabling controller HA")
    return False


def create_new_sg(client):
    """ Creates a new security group"""
    instance_name = os.environ.get('AVIATRIX_TAG')
    vpc_id = os.environ.get('VPC_ID')
    try:
        resp = client.create_security_group(Description='Aviatrix Controller',
                                            GroupName=instance_name,
                                            VpcId=vpc_id)
        sg_id = resp['GroupId']
    except (botocore.exceptions.ClientError, KeyError) as err:
        if "InvalidGroup.Duplicate" in str(err):
            rsp = client.describe_security_groups(GroupNames=[instance_name])
            sg_id = rsp['SecurityGroups'][0]['GroupId']
        else:
            raise AvxError(str(err)) from err
    try:
        client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {'IpProtocol': 'tcp',
                 'FromPort': 443,
                 'ToPort': 443,
                 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp',
                 'FromPort': 80,
                 'ToPort': 80,
                 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
            ])
    except botocore.exceptions.ClientError as err:
        if "InvalidGroup.Duplicate" in str(err) or "InvalidPermission.Duplicate" in str(err):
            pass
        else:
            raise AvxError(str(err)) from err
    return sg_id


def update_env_dict(lambda_client, context, replace_dict):
    """ Update particular variables in the Environment variables in lambda"""
    env_dict = {
        'EIP': os.environ.get('EIP'),
        'AMI_ID': os.environ.get('AMI_ID'),
        'VPC_ID': os.environ.get('VPC_ID'),
        'INST_TYPE': os.environ.get('INST_TYPE'),
        'KEY_NAME': os.environ.get('KEY_NAME'),
        'CTRL_SUBNET': os.environ.get('CTRL_SUBNET'),
        'AVIATRIX_TAG': os.environ.get('AVIATRIX_TAG'),
        'API_PRIVATE_ACCESS': os.environ.get('API_PRIVATE_ACCESS', "False"),
        'PRIV_IP': os.environ.get('PRIV_IP'),
        'INST_ID': os.environ.get('INST_ID'),
        'SUBNETLIST': os.environ.get('SUBNETLIST'),
        'S3_BUCKET_BACK': os.environ.get('S3_BUCKET_BACK'),
        'S3_BUCKET_REGION': os.environ.get('S3_BUCKET_REGION', ''),
        'TOPIC_ARN': os.environ.get('TOPIC_ARN'),
        'NOTIF_EMAIL': os.environ.get('NOTIF_EMAIL'),
        'IAM_ARN': os.environ.get('IAM_ARN'),
        'MONITORING': os.environ.get('IAM_ARN'),
        'DISKS': os.environ.get('DISKS'),
        'TAGS': os.environ.get('TAGS', '[]'),
        'TMP_SG_GRP': os.environ.get('TMP_SG_GRP', ''),
        # 'AVIATRIX_USER_BACK': os.environ.get('AVIATRIX_USER_BACK'),
        # 'AVIATRIX_PASS_BACK': os.environ.get('AVIATRIX_PASS_BACK'),
    }
    env_dict.update(replace_dict)
    os.environ.update(replace_dict)

    lambda_client.update_function_configuration(FunctionName=context.function_name,
                                                Environment={'Variables': env_dict})
    print("Updated environment dictionary")


def login_to_controller(ip_addr, username, pwd):
    """ Logs into the controller and returns the cid"""
    base_url = "https://" + ip_addr + "/v1/api"
    url = base_url + "?action=login&username=" + username + "&password=" + \
          urllib.parse.quote(pwd, '%')
    try:
        response = requests.get(url, verify=False)
    except Exception as err:
        print("Can't connect to controller with elastic IP %s. %s" % (ip_addr,
                                                                      str(err)))
        raise AvxError(str(err)) from err
    response_json = response.json()
    print(response_json)
    try:
        cid = response_json['CID']
        print("Created new session with CID {}\n".format(cid))
    except KeyError as err:
        print("Unable to create session. {}".format(err))
        raise AvxError("Unable to create session. {}".format(err)) from err
    return cid


def set_environ(client, lambda_client, controller_instanceobj, context,
                eip=None):
    """ Sets Environment variables """
    if eip is None:
        # From cloud formation. EIP is not known at this point. So get from controller inst
        eip = controller_instanceobj[
            'NetworkInterfaces'][0]['Association'].get('PublicIp')
    else:
        eip = os.environ.get('EIP')
    sns_topic_arn = os.environ.get('TOPIC_ARN')
    inst_id = controller_instanceobj['InstanceId']
    ami_id = controller_instanceobj['ImageId']
    vpc_id = controller_instanceobj['VpcId']
    inst_type = controller_instanceobj['InstanceType']
    keyname = controller_instanceobj.get('KeyName', '')
    ctrl_subnet = controller_instanceobj['SubnetId']
    priv_ip = controller_instanceobj.get('NetworkInterfaces')[0].get('PrivateIpAddress')
    iam_arn = controller_instanceobj.get('IamInstanceProfile', {}).get('Arn', '')
    mon_bool = controller_instanceobj.get('Monitoring', {}).get('State', 'disabled') != 'disabled'
    monitoring = 'enabled' if mon_bool else 'disabled'
    tags = controller_instanceobj.get("Tags", [])
    tags_stripped = []
    for tag in tags:
        key = tag.get("Key", "")
        # Tags starting with aws: is reserved
        if not key.startswith("aws:"):
            tags_stripped.append(tag)

    disks = []
    for volume in controller_instanceobj.get('BlockDeviceMappings', {}):
        ebs = volume.get('Ebs', {})
        if ebs.get('Status', 'detached') == 'attached':
            vol_id = ebs.get('VolumeId')
            vol = client.describe_volumes(VolumeIds=[vol_id])['Volumes'][0]
            disks.append({"VolumeId": vol_id,
                          "DeleteOnTermination": ebs.get('DeleteOnTermination'),
                          "VolumeType": vol["VolumeType"],
                          "Size": vol["Size"],
                          "Iops": vol.get("Iops", ""),
                          "Encrypted": vol["Encrypted"],
                          })

    env_dict = {
        'EIP': eip,
        'AMI_ID': ami_id,
        'VPC_ID': vpc_id,
        'INST_TYPE': inst_type,
        'KEY_NAME': keyname,
        'CTRL_SUBNET': ctrl_subnet,
        'AVIATRIX_TAG': os.environ.get('AVIATRIX_TAG'),
        'API_PRIVATE_ACCESS': os.environ.get('API_PRIVATE_ACCESS', "False"),
        'PRIV_IP': priv_ip,
        'INST_ID': inst_id,
        'SUBNETLIST': os.environ.get('SUBNETLIST'),
        'S3_BUCKET_BACK': os.environ.get('S3_BUCKET_BACK'),
        'S3_BUCKET_REGION': os.environ.get('S3_BUCKET_REGION', ''),
        'TOPIC_ARN': sns_topic_arn,
        'NOTIF_EMAIL': os.environ.get('NOTIF_EMAIL'),
        'IAM_ARN': iam_arn,
        'MONITORING': monitoring,
        'DISKS': json.dumps(disks),
        'TAGS': json.dumps(tags_stripped),
        'TMP_SG_GRP': os.environ.get('TMP_SG_GRP', ''),
        # 'AVIATRIX_USER_BACK': os.environ.get('AVIATRIX_USER_BACK'),
        # 'AVIATRIX_PASS_BACK': os.environ.get('AVIATRIX_PASS_BACK'),
        }
    print("Setting environment %s" % env_dict)

    lambda_client.update_function_configuration(FunctionName=context.function_name,
                                                Environment={'Variables': env_dict})
    os.environ.update(env_dict)


def verify_iam(controller_instanceobj):
    """ Verify IAM roles"""
    print("Verifying IAM roles ")
    iam_arn = controller_instanceobj.get('IamInstanceProfile', {}).get('Arn', '')
    if not iam_arn:
        return False
    return True


def verify_bucket(controller_instanceobj):
    """ Verify S3 and controller account credentials """
    print("Verifying bucket")
    try:
        s3_client = boto3.client('s3')
        resp = s3_client.get_bucket_location(Bucket=os.environ.get('S3_BUCKET_BACK'))
    except Exception as err:
        print("S3 bucket used for backup is not "
              "valid. %s" % str(err))
        return False, ""
    try:
        bucket_region = resp['LocationConstraint']
    except KeyError:
        print("Key LocationConstraint not found in get_bucket_location response %s" % resp)
        return False, ""

    print("S3 bucket is valid.")
    eip = controller_instanceobj[
        'NetworkInterfaces'][0]['Association'].get('PublicIp')
    print(eip)

    # login_to_controller(eip, os.environ.get('AVIATRIX_USER_BACK'),
    #                     os.environ.get('AVIATRIX_PASS_BACK'))
    return True, bucket_region


def is_backup_file_is_recent(backup_file):
    """ Check if backup file is not older than MAXIMUM_BACKUP_AGE """
    try:
        s3c = boto3.client('s3', region_name=os.environ['S3_BUCKET_REGION'])
        try:
            file_obj = s3c.get_object(Key=backup_file, Bucket=os.environ.get('S3_BUCKET_BACK'))
        except botocore.exceptions.ClientError as err:
            print(str(err))
            return False
        age = time.time() - file_obj['LastModified'].timestamp()
        if age < MAXIMUM_BACKUP_AGE:
            print("Succesfully validated Backup file age")
            return True
        print(f"File age {age} is older than the maximum allowed value of {MAXIMUM_BACKUP_AGE}")
        return False
    except Exception as err:
        print(f"Checking backup file age failed due to {str(err)}")
        return False


def verify_backup_file(controller_instanceobj):
    """ Verify if s3 file exists"""
    print("Verifying Backup file")
    try:
        s3c = boto3.client('s3', region_name=os.environ['S3_BUCKET_REGION'])
        priv_ip = controller_instanceobj['NetworkInterfaces'][0]['PrivateIpAddress']
        version_file = "CloudN_" + priv_ip + "_save_cloudx_version.txt"
        retrieve_controller_version(version_file)
        s3_file = "CloudN_" + priv_ip + "_save_cloudx_config.enc"
        try:
            with open('/tmp/tmp.enc', 'wb') as data:
                s3c.download_fileobj(os.environ.get('S3_BUCKET_BACK'), s3_file, data)
        except botocore.exceptions.ClientError as err:
            if err.response['Error']['Code'] == "404":
                print("The object %s does not exist." % s3_file)
                return False, ""
            print(str(err))
            return False, ""
    except Exception as err:
        print("Verify Backup failed %s" % str(err))
        return False, ""
    else:
        return True, s3_file


def retrieve_controller_version(version_file):
    """ Get the controller version from backup file"""
    print("Retrieving version from file " + str(version_file))
    s3c = boto3.client('s3', region_name=os.environ['S3_BUCKET_REGION'])
    try:
        with open('/tmp/version_ctrlha.txt', 'wb') as data:
            s3c.download_fileobj(os.environ.get('S3_BUCKET_BACK'), version_file,
                                 data)
    except botocore.exceptions.ClientError as err:
        if err.response['Error']['Code'] == "404":
            print("The object does not exist.")
            raise AvxError("The cloudx version file does not exist") from err
        raise
    if not os.path.exists('/tmp/version_ctrlha.txt'):
        raise AvxError("Unable to open version file")
    with open("/tmp/version_ctrlha.txt") as fileh:
        buf = fileh.read()
    print("Retrieved version " + str(buf))
    if not buf:
        raise AvxError("Version file is empty")
    print("Parsing version")
    try:
        ctrl_version = ".".join(((buf[12:]).split("."))[:-1])
    except (KeyboardInterrupt, IndexError, ValueError) as err:
        raise AvxError("Could not decode version") from err
    else:
        print("Parsed version sucessfully " + str(ctrl_version))
        return ctrl_version


def get_initial_setup_status(ip_addr, cid):
    """ Get status of the initial setup completion execution"""
    print("Checking initial setup")
    base_url = "https://" + ip_addr + "/v1/api"
    post_data = {"CID": cid,
                 "action": "initial_setup",
                 "subaction": "check"}
    try:
        response = requests.post(base_url, data=post_data, verify=False)
    except requests.exceptions.ConnectionError as err:
        print(str(err))
        return {'return': False, 'reason': str(err)}
    return response.json()


def run_initial_setup(ip_addr, cid, ctrl_version):
    """ Boots the fresh controller to the specific version"""
    response_json = get_initial_setup_status(ip_addr, cid)
    if response_json.get('return') is True:
        print("Initial setup is already done. Skipping")
        return True
    post_data = {"CID": cid,
                 "target_version": ctrl_version,
                 "action": "initial_setup",
                 "subaction": "run"}
    print("Trying to run initial setup %s\n" % str(post_data))
    base_url = "https://" + ip_addr + "/v1/api"
    try:
        response = requests.post(base_url, data=post_data, verify=False)
    except requests.exceptions.ConnectionError as err:
        if "Remote end closed connection without response" in str(err):
            print("Server closed the connection while executing initial setup API."
                  " Ignoring response")
            response_json = {'return': True, 'reason': 'Warning!! Server closed the connection'}
        else:
            raise AvxError("Failed to execute initial setup: " + str(err)) from err
    else:
        response_json = response.json()
        # Controllers running 6.4 and above would be unresponsive after initial_setup
    print(response_json)
    time.sleep(INITIAL_SETUP_API_WAIT)
    if response_json.get('return') is True:
        print("Successfully initialized the controller")
    else:
        raise AvxError("Could not bring up the new controller to the "
                       "specific version")
    return False


def temp_add_security_group_access(client, controller_instanceobj, api_private_access):
    """ Temporarily add 0.0.0.0/0 rule in one security group"""
    sgs = [sg_['GroupId'] for sg_ in controller_instanceobj['SecurityGroups']]
    if api_private_access == "True":
        return True, sgs[0]

    if not sgs:
        raise AvxError("No security groups were attached to controller")
    try:
        client.authorize_security_group_ingress(
            GroupId=sgs[0],
            IpPermissions=[{'IpProtocol': 'tcp',
                            'FromPort': 443,
                            'ToPort': 443,
                            'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
                          ])
    except botocore.exceptions.ClientError as err:
        if "InvalidPermission.Duplicate" in str(err):
            return True, sgs[0]
        print(str(err))
        raise
    return False, sgs[0]


def restore_security_group_access(client, sg_id):
    """ Remove 0.0.0.0/0 rule in previously added security group"""
    try:
        client.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{'IpProtocol': 'tcp',
                            'FromPort': 443,
                            'ToPort': 443,
                            'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
                           ])
    except botocore.exceptions.ClientError as err:
        if "InvalidPermission.NotFound" not in str(err) and "InvalidGroup" not in str(err):
            print(str(err))


def handle_login_failure(priv_ip,
                         client, lambda_client, controller_instanceobj, context,
                         eip):
    """ Handle login failure through private IP"""
    print("Checking for backup file")
    new_version_file = "CloudN_" + priv_ip + "_save_cloudx_version.txt"
    try:
        retrieve_controller_version(new_version_file)
    except Exception as err:
        print(str(err))
        print("Could not retrieve new version file. Stopping instance. ASG will terminate and "
              "launch a new instance")
        inst_id = controller_instanceobj['InstanceId']
        print("Stopping %s" % inst_id)
        client.stop_instances(InstanceIds=[inst_id])
    else:
        print("Successfully retrieved version. Previous restore operation had succeeded. "
              "Previous lambda may have exceeded 5 min. Updating lambda config")
        set_environ(client, lambda_client, controller_instanceobj, context, eip)


def enable_t2_unlimited(client, inst_id):
    """ Modify instance credit to unlimited for T2 """
    print("Enabling T2 unlimited for %s" % inst_id)
    try:
        client.modify_instance_credit_specification(ClientToken=inst_id,
                                                    InstanceCreditSpecifications=[{
                                                        'InstanceId': inst_id,
                                                        'CpuCredits': 'unlimited'}])
    except botocore.exceptions.ClientError as err:
        print(str(err))


def create_cloud_account(cid, controller_ip, account_name):
    """ Create a temporary account to restore the backup"""
    print("Creating temporary account")
    client = boto3.client('sts')
    aws_acc_num = client.get_caller_identity()["Account"]
    base_url = "https://%s/v1/api" % controller_ip
    post_data = {"CID": cid,
                 "action": "setup_account_profile",
                 "account_name": account_name,
                 "aws_account_number": aws_acc_num,
                 "aws_role_arn": "arn:aws:iam::%s:role/aviatrix-role-app" % aws_acc_num,
                 "aws_role_ec2": "arn:aws:iam::%s:role/aviatrix-role-ec2" % aws_acc_num,
                 "cloud_type": 1,
                 "aws_iam": "true"}
    print("Trying to create account with data %s\n" % str(post_data))
    try:
        response = requests.post(base_url, data=post_data, verify=False)
    except requests.exceptions.ConnectionError as err:
        if "Remote end closed connection without response" in str(err):
            print("Server closed the connection while executing create account API."
                  " Ignoring response")
            output = {"return": True, 'reason': 'Warning!! Server closed the connection'}
            time.sleep(INITIAL_SETUP_DELAY)
        else:
            output = {"return": False, "reason": str(err)}
    else:
        output = response.json()

    return output


def restore_backup(cid, controller_ip, s3_file, account_name):
    """ Restore backup from the s3 bucket"""
    restore_data = {
        "CID": cid,
        "action": "restore_cloudx_config",
        "cloud_type": "1",
        "account_name": account_name,
        "file_name": s3_file,
        "bucket_name": os.environ.get('S3_BUCKET_BACK')}
    print("Trying to restore config with data %s\n" % str(restore_data))
    base_url = "https://" + controller_ip + "/v1/api"
    try:
        response = requests.post(base_url, data=restore_data, verify=False)
    except requests.exceptions.ConnectionError as err:
        if "Remote end closed connection without response" in str(err):
            print("Server closed the connection while executing restore_cloudx_config API."
                  " Ignoring response")
            response_json = {"return": True, 'reason': 'Warning!! Server closed the connection'}
        else:
            print(str(err))
            response_json = {"return": False, "reason": str(err)}
    else:
        response_json = response.json()

    return response_json


def set_customer_id(cid, controller_api_ip):
    """ Set the customer ID if set in environment to migrate to a different AMI type"""
    print("Setting up Customer ID")
    base_url = "https://" + controller_api_ip + "/v1/api"
    post_data = {"CID": cid,
                 "action": "setup_customer_id",
                 "customer_id": os.environ.get("CUSTOMER_ID")}
    try:
        response = requests.post(base_url, data=post_data, verify=False)
    except requests.exceptions.ConnectionError as err:
        if "Remote end closed connection without response" in str(err):
            print("Server closed the connection while executing setup_customer_id API."
                  " Ignoring response")
            response_json = {"return": True, 'reason': 'Warning!! Server closed the connection'}
            time.sleep(WAIT_DELAY)
        else:
            response_json = {"return": False, "reason": str(err)}
    else:
        response_json = response.json()

    if response_json.get('return') is True:
        print("Customer ID successfully programmed")
    else:
        print("Customer ID programming failed. DB restore will fail: " +
              response_json.get('reason', ""))


def handle_ha_event(client, lambda_client, controller_instanceobj, context):
    """ Restores the backup by doing the following
    1. Login to new controller
    2. Assign the EIP to the new controller
    3. Run initial setup to boot to specific version parsed from backup
    4. Login again and restore the configuration """
    old_inst_id = os.environ.get('INST_ID')
    if old_inst_id == controller_instanceobj['InstanceId']:
        print("Controller is already saved. Not restoring")
        return
    if not assign_eip(client, controller_instanceobj, os.environ.get('EIP')):
        raise AvxError("Could not assign EIP")
    eip = os.environ.get('EIP')
    api_private_access = os.environ.get('API_PRIVATE_ACCESS')
    new_private_ip = controller_instanceobj.get(
        'NetworkInterfaces')[0].get('PrivateIpAddress')
    print("New Private IP " + str(new_private_ip))
    if api_private_access == "True":
        controller_api_ip = new_private_ip
        print("API Access to Controller will use Private IP : " + str(controller_api_ip))
    else:
        controller_api_ip = eip
        print("API Access to Controller will use Public IP : " + str(controller_api_ip))

    threading.Thread(target=enable_t2_unlimited,
                     args=[client, controller_instanceobj['InstanceId']]).start()
    duplicate, sg_modified = temp_add_security_group_access(client, controller_instanceobj,
                                                            api_private_access)
    print("0.0.0.0:443/0 rule is %s present %s" %
          ("already" if duplicate else "not",
           "" if duplicate else ". Modified Security group %s" % sg_modified))

    priv_ip = os.environ.get('PRIV_IP')  # This private IP belongs to older terminated instance
    s3_file = "CloudN_" + priv_ip + "_save_cloudx_config.enc"

    if not is_backup_file_is_recent(s3_file):
        raise AvxError(f"HA event failed. Backup file does not exist or is older"
                       f" than {MAXIMUM_BACKUP_AGE}")

    try:
        if not duplicate:
            update_env_dict(lambda_client, context, {'TMP_SG_GRP': sg_modified})
        total_time = 0
        while total_time <= MAX_LOGIN_TIMEOUT:
            try:
                cid = login_to_controller(controller_api_ip, "admin", new_private_ip)
            except Exception as err:
                print(str(err))
                print("Login failed, trying again in " + str(WAIT_DELAY))
                total_time += WAIT_DELAY
                time.sleep(WAIT_DELAY)
            else:
                break
        if total_time >= MAX_LOGIN_TIMEOUT:
            print("Could not login to the controller. Attempting to handle login failure")
            handle_login_failure(controller_api_ip, client, lambda_client, controller_instanceobj,
                                 context, eip)
            return

        version_file = "CloudN_" + priv_ip + "_save_cloudx_version.txt"
        ctrl_version = retrieve_controller_version(version_file)

        initial_setup_complete = run_initial_setup(controller_api_ip, cid, ctrl_version)

        temp_acc_name = "tempacc"

        total_time = 0
        sleep = False
        created_temp_acc = False
        login_complete = False
        response_json = {}
        while total_time <= INITIAL_SETUP_WAIT:
            if sleep:
                print("Waiting for safe initial setup completion, maximum of " +
                      str(INITIAL_SETUP_WAIT - total_time) + " seconds remaining")
                time.sleep(WAIT_DELAY)
            else:
                print(f"{INITIAL_SETUP_WAIT - total_time} seconds remaining")
                sleep = True
            if not login_complete:
                # Need to login again as initial setup invalidates cid after waiting
                print("Logging in again")
                try:
                    cid = login_to_controller(controller_api_ip, "admin", new_private_ip)
                except AvxError:  # It might not succeed since apache2 could restart
                    print("Cannot connect to the controller")
                    sleep = False
                    time.sleep(INITIAL_SETUP_DELAY)
                    total_time += INITIAL_SETUP_DELAY
                    continue
                else:
                    login_complete = True
            if not initial_setup_complete:
                response_json = get_initial_setup_status(controller_api_ip, cid)
                print("Initial setup status %s" % response_json)
                if response_json.get('return', False) is True:
                    initial_setup_complete = True
            if initial_setup_complete and not created_temp_acc:
                response_json = create_cloud_account(cid, controller_api_ip, temp_acc_name)
                print(response_json)
                if response_json.get('return', False) is True:
                    created_temp_acc = True
                elif "already exists" in response_json.get('reason', ''):
                    created_temp_acc = True
            if created_temp_acc and initial_setup_complete:
                if os.environ.get("CUSTOMER_ID"):  # Support for license migration scenario
                    set_customer_id(cid, controller_api_ip)
                response_json = restore_backup(cid, controller_api_ip, s3_file, temp_acc_name)
                print(response_json)
            if response_json.get('return', False) is True and created_temp_acc:
                # If restore succeeded, update private IP to that of the new
                #  instance now.
                print("Successfully restored backup. Updating lambda configuration")
                set_environ(client, lambda_client, controller_instanceobj, context, eip)
                print("Updated lambda configuration")
                print("Controller HA event has been successfully handled")
                return
            if response_json.get('reason', '') == 'account_password required.':
                print("API is not ready yet, requires account_password")
                total_time += WAIT_DELAY
            elif response_json.get('reason', '') == 'valid action required':
                print("API is not ready yet")
                total_time += WAIT_DELAY
            elif response_json.get('reason', '') == 'CID is invalid or expired.' or \
                    "Invalid session. Please login again." in response_json.get('reason', '') or\
                    f"Session {cid} not found" in response_json.get('reason', '') or \
                    f"Session {cid} expired" in response_json.get('reason', ''):
                print("Service abrupty restarted")
                sleep = False
                try:
                    cid = login_to_controller(controller_api_ip, "admin", new_private_ip)
                except AvxError:
                    pass
            elif response_json.get('reason', '') == 'not run':
                print('Initial setup not complete..waiting')
                time.sleep(INITIAL_SETUP_DELAY)
                total_time += INITIAL_SETUP_DELAY
                sleep = False
            elif 'Remote end closed connection without response' in response_json.get('reason', ''):
                print('Remote side closed the connection..waiting')
                time.sleep(INITIAL_SETUP_DELAY)
                total_time += INITIAL_SETUP_DELAY
                sleep = False
            elif "Failed to establish a new connection" in response_json.get('reason', '')\
                    or "Max retries exceeded with url" in response_json.get('reason', ''):
                print('Failed to connect to the controller')
                total_time += WAIT_DELAY
            else:
                print("Restoring backup failed due to " +
                      str(response_json.get('reason', '')))
                return
        raise AvxError("Restore failed, did not update lambda config")
    finally:
        if not duplicate:
            print("Reverting sg %s" % sg_modified)
            update_env_dict(lambda_client, context, {'TMP_SG_GRP': ''})
            restore_security_group_access(client, sg_modified)


def assign_eip(client, controller_instanceobj, eip):
    """ Assign the EIP to the new instance"""
    cf_req = False
    try:
        if eip is None:
            cf_req = True
            eip = controller_instanceobj['NetworkInterfaces'][0]['Association'].get('PublicIp')
        eip_alloc_id = client.describe_addresses(
            PublicIps=[eip]).get('Addresses')[0].get('AllocationId')
        client.associate_address(AllocationId=eip_alloc_id,
                                 InstanceId=controller_instanceobj['InstanceId'])
    except Exception as err:
        if cf_req and "InvalidAddress.NotFound" in str(err):
            print("EIP %s was not found. Please attach an EIP to the controller before enabling HA"
                  % eip)
            return False
        print("Failed in assigning EIP %s" % str(err))
        return False
    else:
        print("Assigned/verified elastic IP")
        return True


def validate_keypair(key_name):
    """ Validates Keypairs"""
    try:
        client = boto3.client('ec2')
        response = client.describe_key_pairs()
    except botocore.exceptions.ClientError as err:
        raise AvxError(str(err)) from err
    key_aws_list = [key['KeyName'] for key in response['KeyPairs']]
    if key_name not in key_aws_list:
        print("Key does not exist. Creating")
        try:
            client = boto3.client('ec2')
            client.create_key_pair(KeyName=key_name)
        except botocore.exceptions.ClientError as err:
            raise AvxError(str(err)) from err
    else:
        print("Key exists")


def validate_subnets(subnet_list):
    """ Validates subnets"""
    vpc_id = os.environ.get('VPC_ID')
    if not vpc_id:
        print("New creation. Assuming subnets are valid as selected from CFT")
        return ",".join(subnet_list)
    try:
        client = boto3.client('ec2')
        response = client.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    except botocore.exceptions.ClientError as err:
        raise AvxError(str(err)) from err
    sub_aws_list = [sub['SubnetId'] for sub in response['Subnets']]
    sub_list_new = [sub for sub in subnet_list if sub.strip() in sub_aws_list]
    if not sub_list_new:
        ctrl_subnet = os.environ.get('CTRL_SUBNET')
        if ctrl_subnet not in sub_aws_list:
            raise AvxError("All subnets %s or controller subnet %s are not found in vpc %s")
        print("All subnets are invalid. Using existing controller subnet")
        return ctrl_subnet
    return ",".join(sub_list_new)


def setup_ha(ami_id, inst_type, inst_id, key_name, sg_list, context,
             attach_instance=True):
    """ Setup HA """
    print("HA config ami_id %s, inst_type %s, inst_id %s, key_name %s, sg_list %s, "
          "attach_instance %s" % (ami_id, inst_type, inst_id, key_name, sg_list, attach_instance))
    lc_name = asg_name = sns_topic = os.environ.get('AVIATRIX_TAG')
    # AMI_NAME = LC_NAME
    # ami_id = client.describe_images(
    #     Filters=[{'Name': 'name','Values':
    #  [AMI_NAME]}],Owners=['self'])['Images'][0]['ImageId']
    asg_client = boto3.client('autoscaling')
    sub_list = os.environ.get('SUBNETLIST')
    val_subnets = validate_subnets(sub_list.split(","))
    print("Valid subnets %s" % val_subnets)
    if key_name:
        validate_keypair(key_name)
    bld_map = []
    try:
        tags = json.loads(os.environ.get('TAGS'))
    except ValueError:
        tags = [{'Key': 'Name', 'Value': asg_name, 'PropagateAtLaunch': True}]
    else:
        if not tags:
            tags = [{'Key': 'Name', 'Value': asg_name, 'PropagateAtLaunch': True}]
        for tag in tags:
            tag['PropagateAtLaunch'] = True

    disks = json.loads(os.environ.get('DISKS'))
    if disks:
        for disk in disks:
            disk_config = {"Ebs": {"VolumeSize": disk["Size"],
                                   "VolumeType": disk['VolumeType'],
                                   "DeleteOnTermination": disk['DeleteOnTermination'],
                                   # "Encrypted": disk["Encrypted"],  # Encrypted cannot be set
                                   #  since snapshot is specified
                                   "Iops": disk.get("Iops", '')},
                           'DeviceName': '/dev/sda1'}
            if not disk_config["Ebs"]["Iops"]:
                del disk_config["Ebs"]["Iops"]
            bld_map.append(disk_config)

    if not bld_map:
        print("bld map is empty")
        raise AvxError("Could not find any disks attached to the controller")

    if inst_id:
        print("Setting launch config from instance")
        asg_client.create_launch_configuration(
            LaunchConfigurationName=lc_name,
            ImageId=ami_id,
            InstanceId=inst_id,
            BlockDeviceMappings=bld_map,
            UserData="# Ignore"
        )
    else:
        print("Setting launch config from environment")
        iam_arn = os.environ.get('IAM_ARN')
        monitoring = os.environ.get('MONITORING', 'disabled') == 'enabled'

        kw_args = {
            "LaunchConfigurationName": lc_name,
            "ImageId": ami_id,
            "InstanceType": inst_type,
            "SecurityGroups": sg_list,
            "KeyName": key_name,
            "AssociatePublicIpAddress": True,
            "InstanceMonitoring": {"Enabled": monitoring},
            "BlockDeviceMappings": bld_map,
            "UserData": "# Ignore",
            "IamInstanceProfile": iam_arn,
        }
        if not key_name:
            del kw_args["KeyName"]
        if not iam_arn:
            del kw_args["IamInstanceProfile"]
        if not bld_map:
            del kw_args["BlockDeviceMappings"]
        asg_client.create_launch_configuration(**kw_args)
    tries = 0
    while tries < 3:
        try:
            print("Trying to create ASG")
            asg_client.create_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                LaunchConfigurationName=lc_name,
                MinSize=0,
                MaxSize=1,
                DesiredCapacity=0 if attach_instance else 1,
                VPCZoneIdentifier=val_subnets,
                Tags=tags
            )
        except botocore.exceptions.ClientError as err:
            if "AlreadyExists" in str(err):
                print("ASG already exists")
                if "pending delete" in str(err):
                    print("Pending delete. Trying again in 10 secs")
                    time.sleep(10)
            else:
                raise
        else:
            break

    print('Created ASG')
    if attach_instance:
        asg_client.attach_instances(InstanceIds=[inst_id],
                                    AutoScalingGroupName=asg_name)
    sns_client = boto3.client('sns')
    sns_topic_arn = sns_client.create_topic(Name=sns_topic).get('TopicArn')
    os.environ['TOPIC_ARN'] = sns_topic_arn
    print('Created SNS topic %s' % sns_topic_arn)
    lambda_client = boto3.client('lambda')
    update_env_dict(lambda_client, context, {'TOPIC_ARN': sns_topic_arn})
    lambda_fn_arn = lambda_client.get_function(
        FunctionName=context.function_name).get('Configuration').get(
            'FunctionArn')
    sns_client.subscribe(TopicArn=sns_topic_arn,
                         Protocol='lambda',
                         Endpoint=lambda_fn_arn).get('SubscriptionArn')
    if os.environ.get('NOTIF_EMAIL'):
        try:
            sns_client.subscribe(TopicArn=sns_topic_arn,
                                 Protocol='email',
                                 Endpoint=os.environ.get('NOTIF_EMAIL'))
        except botocore.exceptions.ClientError as err:
            print("Could not add email notification %s" % str(err))
    else:
        print("Not adding email notification")
    lambda_client.add_permission(FunctionName=context.function_name,
                                 StatementId=str(uuid.uuid4()),
                                 Action='lambda:InvokeFunction',
                                 Principal='sns.amazonaws.com',
                                 SourceArn=sns_topic_arn)
    print('SNS topic: Added lambda subscription.')
    asg_client.put_notification_configuration(
        AutoScalingGroupName=asg_name,
        NotificationTypes=['autoscaling:EC2_INSTANCE_LAUNCH',
                           'autoscaling:EC2_INSTANCE_LAUNCH_ERROR'],
        TopicARN=sns_topic_arn)
    print('Attached ASG')


def delete_resources(inst_id, delete_sns=True, detach_instances=True):
    """ Cloud formation cleanup"""
    lc_name = asg_name = os.environ.get('AVIATRIX_TAG')

    asg_client = boto3.client('autoscaling')
    if detach_instances:
        try:
            asg_client.detach_instances(
                InstanceIds=[inst_id],
                AutoScalingGroupName=asg_name,
                ShouldDecrementDesiredCapacity=True)
            print("Controller instance detached from autoscaling group")
        except botocore.exceptions.ClientError as err:
            print(str(err))
    try:
        asg_client.delete_auto_scaling_group(AutoScalingGroupName=asg_name,
                                             ForceDelete=True)
    except botocore.exceptions.ClientError as err:
        if "AutoScalingGroup name not found" in str(err):
            print('ASG already deleted')
        else:
            raise AvxError(str(err)) from err
    print("Autoscaling group deleted")
    try:
        asg_client.delete_launch_configuration(LaunchConfigurationName=lc_name)
    except botocore.exceptions.ClientError as err:
        if "Launch configuration name not found" in str(err):
            print('LC already deleted')
        else:
            print(str(err))
    print("Launch configuration deleted")
    if delete_sns:
        print("Deleting SNS topic")
        sns_client = boto3.client('sns')
        topic_arn = os.environ.get('TOPIC_ARN')
        if topic_arn == "N/A" or not topic_arn:
            print("Topic not created. Exiting")
            return
        try:
            response = sns_client.list_subscriptions_by_topic(TopicArn=topic_arn)
        except botocore.exceptions.ClientError as err:
            print('Could not delete topic due to %s' % str(err))
        else:
            for subscription in response.get('Subscriptions', []):
                try:
                    sns_client.unsubscribe(SubscriptionArn=subscription.get('SubscriptionArn', ''))
                except botocore.exceptions.ClientError as err:
                    print(str(err))
            print("Deleted subscriptions")
        try:
            sns_client.delete_topic(TopicArn=topic_arn)
        except botocore.exceptions.ClientError as err:
            print('Could not delete topic due to %s' % str(err))
        else:
            print("SNS topic deleted")


def send_response(event, context, response_status, reason='',
                  response_data=None, physical_resource_id=None):
    """ Send response to cloud formation template for custom resource creation
     by cloud formation"""

    response_data = response_data or {}
    response_body = json.dumps(
        {
            'Status': response_status,
            'Reason': str(reason) + ". See the details in CloudWatch Log Stream: " +
                      context.log_stream_name,
            'PhysicalResourceId': physical_resource_id or context.log_stream_name,
            'StackId': event['StackId'],
            'RequestId': event['RequestId'],
            'LogicalResourceId': event['LogicalResourceId'],
            'Data': response_data
        }
    )
    opener = build_opener(HTTPHandler)
    request = Request(event['ResponseURL'], data=response_body.encode())
    request.add_header('Content-Type', '')
    request.add_header('Content-Length', len(response_body.encode()))
    request.get_method = lambda: 'PUT'
    try:
        response = opener.open(request)
        print("Status code: {}".format(response.getcode()))
        print("Status message: {}".format(response.msg))
        return True
    except HTTPError as exc:
        print("Failed executing HTTP request: {}".format(exc.code))
        return False
