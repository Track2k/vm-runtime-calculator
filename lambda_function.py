#!/usr/bin/env python3

import boto3
import datetime
import csv
import time
import io
from datetime import timezone, timedelta
import logging
import os

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
S3_BUCKET = os.environ.get('S3_BUCKET')  # Replace with your S3 bucket name
S3_KEY = os.environ.get('S3_KEY')  # S3 key path for the report
REGION = os.environ.get('REGION') 
HOUR_THRESHOLD = 0   # Report instances running longer than this many hours
LOOKBACK_DAYS = 7     # How many days to check history

# SES Configuration
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')  # Replace with a verified email in SES
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')  # Replace with recipient email

# Initialize SES client
ses_client = boto3.client("ses", region_name=REGION)

def send_email(report_link):
    """
    Send an email via AWS SES with the report link.
    """
    subject = "AWS EC2 Runtime Report"
    body_text = f"The EC2 instance runtime report has been generated.\n\nDownload it here: {report_link}"
    
    try:
        response = ses_client.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [RECIPIENT_EMAIL]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body_text}}
            },
        )
        logger.info(f"Email sent successfully! Message ID: {response['MessageId']}")
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")


def get_all_instances():
    """
    Get all EC2 instances regardless of state
    """
    print("Getting all EC2 instances...")
    
    try:
        # Connect to EC2
        ec2 = boto3.client('ec2', region_name=REGION)
        
        # Get all instances
        response = ec2.describe_instances()
        
        # Create a list to store instance info
        all_instances = []
        
        # Loop through the response and extract instance details
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                # Get instance name from tags
                instance_name = "Unnamed"
                if 'Tags' in instance:
                    for tag in instance['Tags']:
                        if tag['Key'] == 'Name':
                            instance_name = tag['Value']
                
                # Create dictionary with instance info
                instance_info = {
                    'InstanceId': instance['InstanceId'],
                    'Name': instance_name,
                    'Type': instance['InstanceType'],
                    'State': instance['State']['Name'],
                    'CurrentSession': 0
                }
                
                # If instance is running, calculate current session time
                if instance['State']['Name'] == 'running':
                    launch_time = instance['LaunchTime']
                    current_time = datetime.datetime.now(timezone.utc)
                    running_time = current_time - launch_time
                    instance_info['CurrentSession'] = running_time.total_seconds() / 3600  # Convert to hours
                
                # Add to our list
                all_instances.append(instance_info)
        
        print(f"Found {len(all_instances)} instances.")
        return all_instances
    
    except Exception as e:
        print(f"Error getting instances: {str(e)}")
        return []

def get_cumulative_runtime(instance_id):
    """
    Calculate cumulative runtime for an instance using CloudWatch metrics
    """
    try:
        # Connect to CloudWatch
        cloudwatch = boto3.client('cloudwatch', region_name=REGION)
        
        # Calculate time period
        end_time = datetime.datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=LOOKBACK_DAYS)
        print(start_time)
        
        # Get CPU Utilization data points
        # This is an effective way to check if an instance was running
        # because CloudWatch only collects metrics when the instance is active
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='CPUUtilization',
            Dimensions=[
                {
                    'Name': 'InstanceId',
                    'Value': instance_id
                },
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1-hour intervals
            Statistics=['Average']
        )
        
        # Count data points (each represents an hour the instance was running)
        runtime_hours = len(response['Datapoints'])
        
        return runtime_hours
    
    except Exception as e:
        print(f"Error calculating runtime for {instance_id}: {str(e)}")
        return 0

def calculate_all_runtimes():
    """
    Calculate cumulative runtime for all instances
    """
    print(f"Calculating cumulative runtime over the past {LOOKBACK_DAYS} days...")
    
    # Get all instances
    instances = get_all_instances()
    
    # Track progress
    total = len(instances)
    processed = 0
    
    # Process each instance
    for instance in instances:
        # Track progress
        processed += 1
        print(f"Processing instance {processed}/{total}: {instance['InstanceId']} ({instance['Name']})")
        
        # Calculate cumulative runtime
        cumulative_hours = get_cumulative_runtime(instance['InstanceId'])
        instance['CumulativeHours'] = cumulative_hours
        
        # Sleep briefly to avoid hitting API rate limits
        time.sleep(0.2)
    
    return instances

def generate_report(instances):
    """
    Generate a CSV report, save it to S3, and send an SES email.
    """
    long_running = [i for i in instances if i['CumulativeHours'] > HOUR_THRESHOLD]
    long_running.sort(key=lambda x: x['CumulativeHours'], reverse=True)

    try:
        csv_buffer = io.StringIO()
        fieldnames = ['InstanceId', 'Name', 'Type', 'State', 'CurrentSession', 'CumulativeHours']
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
        writer.writeheader()

        for instance in long_running:
            writer.writerow({
                'InstanceId': instance['InstanceId'],
                'Name': instance['Name'],
                'Type': instance['Type'],
                'State': instance['State'],
                'CurrentSession': round(instance['CurrentSession'], 2),
                'CumulativeHours': instance['CumulativeHours']
            })

        s3 = boto3.resource('s3')
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        s3_key = S3_KEY.replace('.csv', f'_{timestamp}.csv')
        s3.Object(S3_BUCKET, s3_key).put(Body=csv_buffer.getvalue())

        report_link = f"https://{S3_BUCKET}.s3.{REGION}.amazonaws.com/{s3_key}"
        logger.info(f"Report uploaded to S3: {report_link}")

        # Send email notification
        send_email(report_link)

        return {"status": "Success", "message": f"Report saved to {s3_key}, email sent."}

    except Exception as e:
        logger.error(f"Error generating report: {str(e)}")
        return {"status": "Error", "message": str(e)}

        
def lambda_handler(event, context):
    """
    AWS Lambda entry point.
    """
    logger.info("Lambda function started.")
    instances = calculate_all_runtimes()
    
    if instances:
        return generate_report(instances)
    else:
        return {"status": "Error", "message": "No instances found or error occurred."}
