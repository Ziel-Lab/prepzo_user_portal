import os
import boto3
import json
from botocore.exceptions import ClientError

def get_secret(secret_name, region_name="us-east-1"):
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name,
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        print(f"Error fetching secret: {e}")
        raise e
    else:
        if 'SecretString' in get_secret_value_response:
            secret_dict = json.loads(get_secret_value_response['SecretString'])
            for key, value in secret_dict.items():
                os.environ[key] = value  # ✅ Inject into env
            return secret_dict
        else:
            # Optional: handle binary secrets if you use them
            return get_secret_value_response['SecretBinary']
