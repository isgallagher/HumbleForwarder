"""
Name: HumbleForwarder
Author: kjp

Humble SES email forwarder.  All forwarding goes to a single address, feel free to
fork it if you want more configurability.

Massively reworked and cleaned up version of
https://aws.amazon.com/blogs/messaging-and-targeting/forward-incoming-email-to-an-external-destination/

Setup instructions:

* Follow the AWS blog link above, but use this Lambda code (Python 3.8+) instead.

* Make sure your Lambda has a large enough size and timeout, especially if
  you want to test the error emails due to too large of body.

* Consider granting the Lambda `SNS:Publish` permission, and enable its Dead Letter Queue
  so you get notified if any Lambda fails after 3 async attempts.

* Read the configuration options below, then change them in the code or use environment variables

Features:

* Don't forward emails marked as spam / virus.  Note you need to have scanning enabled.

* The body/content is not modified, all attachments are kept

* Send an error email if there was a problem sending (like body too large).
  You can test this by setting the env var TEST_LARGE_BODY, to generate a 20MB body.
  A 768MB Lambda takes 15 seconds for this test.

* JSON logging.  You can run this cloudwatch logs insights query to check on your emails:

     fields @timestamp, input_event.Records.0.ses.mail.commonHeaders.from.0,
        input_event.Records.0.ses.mail.commonHeaders.subject
    | filter input_event.Records.0.eventSource = 'aws:ses'
    | sort @timestamp desc
    | limit 20

Other Thanks:

* Got some inspiration from https://github.com/chrismarcellino/lambda-ses-email-forwarder/

"""

import unittest
import traceback
import json
import os
import logging
import email.policy
import email.parser
import email.message

import boto3
from botocore.exceptions import ClientError

###############################################################################
#
# Required configuration.  Can set here, or in environment var(s), your choice
#
###############################################################################
REGION = os.getenv('Region', 'us-east-1')

INCOMING_EMAIL_BUCKET = os.getenv('MailS3Bucket', 'yourbucketname')

# Don't have a leading or trailing /, it will be added automatically
INCOMING_EMAIL_PREFIX = os.getenv('MailS3Prefix', '')

# If empty, uses the SES recipient address
SENDER = os.getenv('MailSender', '')

RECIPIENT = os.getenv('MailRecipient', 'to@anywhere.com')

###############################################################################
###############################################################################

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info(json.dumps(dict(input_event=event)))

    # Get the unique ID of the message. This corresponds to the name of the file in S3.
    message_id = event['Records'][0]['ses']['mail']['messageId']
    logger.debug(json.dumps(dict(message_id=message_id)))

    # Check for spam / virus
    if is_ses_spam(event):
        logger.error(json.dumps(dict(message="rejecting spam message", message_id=message_id)))
        return

    # These are the valid recipient(s) for your domain.
    # Any other bogus addresses in the To: header should not be present here.
    ses_recipients = get_ses_recipients(event)

    # This loop is inefficient, but optimizing multiple users is not something
    # that's a priority for me.
    for ses_recipient in ses_recipients:
        forward_mail(ses_recipient, message_id)


def forward_mail(ses_recipient, message_id):
    # Retrieve the original message from the S3 bucket.
    message = get_message_from_s3(message_id)

    # Get the complete set of new headers.  This one function is where all the forwarding
    # logic / magic can be contained.
    config = dict(sender=SENDER, recipient=RECIPIENT)
    new_headers = get_new_message_headers(config, ses_recipient, message)

    # Change all the headers now
    set_new_message_headers(message, new_headers)

    # For fault injection (Testing error emails)
    if os.getenv("TEST_LARGE_BODY"):
        logger.info("setting a huge body")
        message.clear_content()
        message.set_content("x" * 20000000)

    if os.getenv("TEST_DEBUG_BODY"):
        logger.info(json.dumps(dict(email_body=message.as_string())))

    # Send the message
    try:
        send_raw_email(message)
    except ClientError as e:
        logger.error("error sending forwarded email", exc_info=True)
        traceback_string = traceback.format_exc()
        error_message = create_error_email(message, traceback_string)
        send_raw_email(error_message)


def get_message_from_s3(message_id):
    # NB: This is dumb, but I'm doing it to stay compatible with the original version
    if INCOMING_EMAIL_PREFIX:
        object_path = (INCOMING_EMAIL_PREFIX + "/" + message_id)
    else:
        object_path = message_id

    # Create a new S3 client.
    client_s3 = boto3.client("s3")

    # Get the email object from the S3 bucket.
    object_s3 = client_s3.get_object(Bucket=INCOMING_EMAIL_BUCKET, Key=object_path)
    raw_bytes = object_s3['Body'].read()
    return parse_message_from_bytes(raw_bytes)


def parse_message_from_bytes(raw_bytes):
    parser = email.parser.BytesParser(policy=email.policy.SMTP)
    return parser.parsebytes(raw_bytes)


def get_new_message_headers(config, ses_recipient, message):
    # NB: This function shouldn't use any global vars, because we want it unit testable.
    new_headers = {}

    # Headers we keep unchanged
    headers_to_keep = [
            "MIME-Version",
            "Content-Type",
            "Content-Disposition",
            "Content-Transfer-Encoding",
            "Date",
            "Subject",
            ]

    for header in headers_to_keep:
        if header in message:
            new_headers[header] = message[header]

    # Headers we customize for forwarding logic
    new_headers["To"] = config["recipient"]

    if config["sender"]:
        new_headers["From"] = config["sender"]
    else:
        new_headers["From"] = ses_recipient

    if message["Reply-To"]:
        new_headers["Reply-To"] = message["Reply-To"]
    else:
        new_headers["Reply-To"] = message["From"]

    return new_headers


def set_new_message_headers(message, new_headers):
    # Clear all headers
    for header in message.keys():
        del message[header]
    for (name, value) in new_headers.items():
        message[name] = value


def create_error_email(attempted_message, traceback_string):
    # Create a new message
    new_message = email.message.EmailMessage(policy=email.policy.SMTP)
    new_message["From"] = attempted_message["From"]
    new_message["To"] = attempted_message["To"]
    new_message["Subject"] = "Email forwarding error"

    text = f"""
There was an error forwarding an email to SES.

Original Sender: {attempted_message["Reply-To"]}
Original Subject: {attempted_message["Subject"]}

Traceback:

{traceback_string}
        """.strip()

    new_message.set_content(text)
    return new_message


def get_ses_recipients(event):
    receipt = event['Records'][0]['ses']['receipt']
    return receipt["recipients"]


def is_ses_spam(event):
    receipt = event['Records'][0]['ses']['receipt']
    verdicts = ['spamVerdict', 'virusVerdict', 'spfVerdict', 'dkimVerdict', 'dmarcVerdict']
    is_fail = False
    for verdict in verdicts:
        if verdict in receipt:
            status = receipt[verdict].get("status")
            logger.debug(json.dumps(dict(verdict=verdict, status=status)))
            if status == "FAIL":
                is_fail = True
    return is_fail


def send_raw_email(message):
    client_ses = boto3.client('ses', REGION)
    response = client_ses.send_raw_email(
            Source=message['From'],
            Destinations=[message['To']],
            RawMessage={
                'Data': message.as_string()
                }
            )
    print("Email sent! MessageId:", response['MessageId'])



class UnitTests(unittest.TestCase):
    def test_multiple_recipients(self):
        with open("tests/multiple_recipients.txt", "rb") as f:
            text = f.read()
        message = parse_message_from_bytes(text)
        # This is why we shouldn't trust the original To header,
        # it can have all kind of junk
        self.assertEqual(len(message["To"].addresses), 3)

    def test_header_changes(self):
        with open("tests/multiple_recipients.txt", "rb") as f:
            text = f.read()
        message = parse_message_from_bytes(text)
        ses_recipient = "code@coder.dev"
        config = dict(sender="", recipient="someone@secret.com")
        new_headers = get_new_message_headers(config, ses_recipient, message)
        self.assertEqual(new_headers["To"], "someone@secret.com")
        self.assertEqual(new_headers["From"], "code@coder.dev")
        self.assertEqual(new_headers["Subject"], "test 3 addresses")
        self.assertEqual(new_headers["Reply-To"], "Alpha Sigma <user@users.com>")
        self.assertEqual("Content-Disposition" in new_headers, False)

        config = dict(sender="fixed@coder.dev", recipient="someone@secret.com")
        new_headers = get_new_message_headers(config, ses_recipient, message)
        self.assertEqual(new_headers["To"], "someone@secret.com")
        self.assertEqual(new_headers["From"], "fixed@coder.dev")

    def test_header_changes2(self):
        with open("tests/reply_to.txt", "rb") as f:
            text = f.read()
        message = parse_message_from_bytes(text)
        ses_recipient = "code@coder.dev"
        config = dict(sender="", recipient="someone@secret.com")
        new_headers = get_new_message_headers(config, ses_recipient, message)
        self.assertEqual(new_headers["Subject"], "test reply-to")
        self.assertEqual(new_headers["Reply-To"], "My Alias <alias@alias.com>")

        set_new_message_headers(message, new_headers)
        self.assertEqual(message["From"], "code@coder.dev")
        self.assertEqual(message["To"], "someone@secret.com")
        self.assertEqual(message["Subject"], "test reply-to")
        self.assertEqual(message["Reply-To"], "My Alias <alias@alias.com>")

    def test_event_parsing(self):
        with open("tests/event.json", "rb") as f:
            event = json.load(f)
        self.assertEqual(is_ses_spam(event), True)
        self.assertEqual(get_ses_recipients(event), ['code@coder.dev', 'code2@coder.dev'])


if __name__ == '__main__':
    unittest.main()
