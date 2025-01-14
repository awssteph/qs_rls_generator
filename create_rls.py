import os

import boto3
import csv
from os import environ as os_environ
from os.path import basename as file_basename
from botocore.exceptions import NoCredentialsError
from sys import exit

OWNER_TAG = os_environ['CUDOS_OWNER_TAG'] if 'CUDOS_OWNER_TAG' in os_environ else 'cudos_users'
BUCKET_NAME = os_environ['BUCKET_NAME'] if 'BUCKET_NAME' in os_environ else exit(
    "Missing bucket for uploading CSV. Please define bucket as ENV VAR BUCKET_NAME")
TMP_RLS_FILE = os_environ['TMP_RLS_FILE'] if 'TMP_RLS_FILE' in os_environ else '/tmp/cudos_rls.csv'
RLS_HEADER = ['UserName', 'account_id']
ROOT_OU = os_environ['ROOT_OU'] if 'ROOT_OU' in os_environ else exit("Missing ROOT_OU env var, please define ROOT_OU in ENV vars")
ACCOUNT_ID = boto3.client('sts').get_caller_identity().get('Account')
QS_REGION = 'eu-central-1'

def assume_managment():                
    management_role_arn = os_environ["MANAGMENTARN"]
    sts_connection = boto3.client('sts')
    acct_b = sts_connection.assume_role(
      RoleArn=management_role_arn,
      RoleSessionName="cross_acct_lambda"
    )
    ACCESS_KEY = acct_b['Credentials']['AccessKeyId']
    SECRET_KEY = acct_b['Credentials']['SecretAccessKey']
    SESSION_TOKEN = acct_b['Credentials']['SessionToken']
    client = boto3.client(
      "organizations", region_name="us-east-1", #Using the Organizations client to get the data. This MUST be us-east-1 regardless of region you have the Lamda in
      aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY, aws_session_token=SESSION_TOKEN, )
    return client

if 'ROLE_ARN' not in os.environ:
    org_client = boto3.client('organizations')
else:
    org_client = assume_managment()
s3_client = boto3.client('s3')
qs_client = boto3.client('quicksight',region_name=QS_REGION)

def get_tags(account_list):
    for index, account  in enumerate(account_list):
        account_tags = org_client.list_tags_for_resource(ResourceId=account["Id"])['Tags']
        account_tags = {'AccountTags': account_tags}
        account.update(account_tags)
        account_list[index] = account
    return account_list


def  print_account_list():
    account_list = remove_inactive_accoutns(org_client.list_accounts()['Accounts'])
    account_list = get_tags(account_list)
    print(account_list)


def add_full_access_users(full_acess_user, cudos_users):
    full_acess_user = full_acess_user.strip()
    if full_acess_user in cudos_users:
        cudos_users[full_acess_user] = ' '
    else:
        cudos_users.update({full_acess_user: ' '})


def add_cudos_user_to_qs_rls(account, users, qs_rls,separator=":"):
    """ Default separator """
    users = users.split(separator)
    for user in users:
        user = user.strip()
        if user in qs_rls.keys():
           if account not in qs_rls[user]:
               qs_rls[user].append(account)
        else:
           qs_rls.update({user: []})
           add_cudos_user_to_qs_rls(account,user, qs_rls)
    return qs_rls


def get_ou_children(ou):
    NextToken = True
    ous_list = []
    while NextToken:
        if NextToken is str:
            list_ous_result = org_client.list_organizational_units_for_parent(ParentId=ou, MaxResults=20, NextToken=NextToken)
        else:
            list_ous_result = org_client.list_organizational_units_for_parent(ParentId=ou, MaxResults=20)
        if 'NextToken' in list_ous_result:
            NextToken = list_ous_result['NextToken']
        else:
            NextToken = False
            ous = list_ous_result['OrganizationalUnits']
            for ou in ous:
                ous_list.append(ou['Id'])
    return ous_list


def get_ou_accounts(ou, accounts_list=None, process_ou_children=True):
    NextToken = True
    if accounts_list is None:
        accounts_list = []
    while NextToken:
        if NextToken is str:
            list_accounts_result = org_client.list_accounts_for_parent(ParentId=ou, MaxResults=20, NextToken=NextToken)
        else:
            list_accounts_result = org_client.list_accounts_for_parent(ParentId=ou, MaxResults=20)
        if 'NextToken' in list_accounts_result:
            NextToken = list_accounts_result['NextToken']
        else:
            NextToken = False
        accounts = list_accounts_result['Accounts']
        for account in accounts:
            if  account['Status'] == 'ACTIVE':
                accounts_list.append(account)
    if process_ou_children:
        for ou in get_ou_children(ou):
            get_ou_accounts(ou, accounts_list)
    return accounts_list


def get_cudos_users(account_list):
    cudos_users = []
    for account in account_list:
        for index, key in enumerate(account['AccountTags']):
            if key['Key'] == 'cudos_users':
                cudos_users.append((account['Id'], account['AccountTags'][index]['Value']))
    return cudos_users


def dict_list_to_csv(dict):
    for key in dict:
        dict[key]=','.join(dict[key])
    return dict


def upload_to_s3(file, s3_file):
    s3 = boto3.client('s3')
    try:
        s3.upload_file(file, BUCKET_NAME, file_basename(s3_file))
    except FileNotFoundError:
        print("The file was not found")
        return None
    except NoCredentialsError:
        print("Credentials not available")
        return None


def main(separator=":"):
    ou_tag_data = {}
    root_ou = ROOT_OU
    ou_tag_data = process_ou(root_ou, ou_tag_data, root_ou)
    ou_tag_data = process_root_ou(root_ou,ou_tag_data)
    print(f"OU_TAG_DATA: {ou_tag_data}")
    qs_users = get_qs_users(ACCOUNT_ID, qs_client)
    qs_users = {qs_user['UserName']: qs_user['Email'] for qs_user in qs_users}
    qs_email_user_map = {}
    for key, value in qs_users.items():
        if value not in qs_email_user_map:
            qs_email_user_map[value] = [key]
        else:
            qs_email_user_map[value].append(key)
    qs_rls = {}
    for entry in ou_tag_data:
        if entry in qs_email_user_map:
            for qs_user in qs_email_user_map[entry]:
                qs_rls[qs_user] = ou_tag_data[entry]
    print("QS EMAIL USER MAPPING: {}".format(qs_email_user_map))
    print("QS RLS DATA: {}".format(qs_rls))
    write_csv(qs_rls)


#    write_csv(qs_rls)

def get_qs_users(account_id,qs_client):
    print("Fetching QS users, Getting first page, NextToken: 0")
    qs_users_result = (qs_client.list_users(AwsAccountId=account_id, MaxResults=100, Namespace='default'))
    qs_users = qs_users_result['UserList']

    while 'NextToken' in qs_users_result:
        NextToken=qs_users_result['NextToken']
        qs_users_result = (qs_client.list_users(AwsAccountId=account_id, MaxResults=100, Namespace='default', NextToken=NextToken))
        qs_users.extend(qs_users_result['UserList'])
        print("Fetching QS users, getting Next Page, NextToken: {}".format(NextToken.split('/')[0]))

    for qs_users_index, qs_user in enumerate(qs_users):
        qs_user = {'UserName': qs_user['UserName'], 'Email': qs_user['Email']}
        qs_users[qs_users_index] = qs_user

    return qs_users


def process_account(account_id, qs_rls, ou):
    print(f"DEBUG: proessing account level tags, processing account_id: {account_id}")
    tags = org_client.list_tags_for_resource(ResourceId=account_id)['Tags']
    for tag in tags:
        if tag['Key'] == 'cudos_users':
            cudos_users_tag_value = tag['Value']
            print(f"DEBUG: processing child account: {account_id} for ou: {ou}")
            add_cudos_user_to_qs_rls(account_id, cudos_users_tag_value, qs_rls)
    return qs_rls


def process_root_ou(root_ou, qs_rls):
    tags = org_client.list_tags_for_resource(ResourceId=root_ou)['Tags']
    for tag in tags:
        if tag['Key'] == 'cudos_users':
            cudos_users_tag_value = tag['Value']
            for user in cudos_users_tag_value.split(':'):
                if user in qs_rls.keys():
                    qs_rls[user] = [' ']
                else:
                    qs_rls.update({user: ' '})
    return qs_rls


def process_ou(ou, qs_rls, root_ou):
    print("DEBUG: processing ou {}".format(ou))
    tags = org_client.list_tags_for_resource(ResourceId=ou)['Tags']
    for tag in tags:
        if tag['Key'] == 'cudos_users':
            cudos_users_tag_value = tag['Value']
            """ Do not process all children if this is root ou, this is done bellow in separate cycle. """
            process_ou_children = bool( ou != root_ou)
            for account in get_ou_accounts(ou, process_ou_children=process_ou_children):
                account_id = account['Id']
                print(f"DEBUG: processing inherit tag: {cudos_users_tag_value} for ou: {ou} account_id: {account_id}")
                add_cudos_user_to_qs_rls(account_id, cudos_users_tag_value, qs_rls)

    children_ou = get_ou_children(ou)
    if len(children_ou) > 0:
        for child_ou in children_ou:
            print(f"DEBUG: processing child ou: {child_ou}")
            process_ou(child_ou, qs_rls,root_ou)

    ou_accounts = get_ou_accounts(ou, process_ou_children=False)  # Do not process children, only accounts at OU level.
    ou_accounts_ids = [ ou_account['Id'] for ou_account in ou_accounts]
    print(f"DEBUG: Getting accounts in  OU: {ou} ########################### ou_accounts:{ou_accounts_ids}")
    for account in ou_accounts:
        account_id = account['Id']
        print(f"DEBUG: Processing OU level accounts for ou: {ou}, account: {account_id}")
        process_account(account_id, qs_rls, ou)
    return qs_rls


def write_csv(qs_rls):
    qs_rls_dict_list = dict_list_to_csv(qs_rls)
    with open(TMP_RLS_FILE,'w',newline='') as cudos_rls_csv_file:
        wrt = csv.DictWriter(cudos_rls_csv_file,fieldnames=RLS_HEADER)
        wrt.writeheader()
        for k,v in qs_rls_dict_list.items():
            wrt.writerow({RLS_HEADER[0]: k, RLS_HEADER[1]: v})
    upload_to_s3(TMP_RLS_FILE, TMP_RLS_FILE)

                
def lambda_handler(event, context):
    main()

if __name__ == '__main__':
    main()