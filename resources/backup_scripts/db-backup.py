#!/usr/bin/env python

###############################################################################
# Database Backup script for Warm Showers
#
# This script will create database dumps for the past seven days, and sync
# those dumps with S3. Old dumps will be deleted locally and on S3.
#
# Dependencies: /usr/bin/drush
# Tested with Python 2.7.12.
#
# Sample cron entry (run once every day at 2:30AM):
#
# 30 2 * * * /var/www/warmshowers.org/resources/backup_scripts/db-backup.py
#
#

import os
import glob
import json
import re
import argparse
from datetime import date, timedelta
from collections import namedtuple

def get_settings():
    # Default settings
    s3_bucket = 'warmshowers-database-backup'
    backup_dir = '/var/backups/db_backups'
    docroot_dir = '/var/www/warmshowers.org/docroot'
    dry_run  = False
    force_dump  = False

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='Sync database dumps from the last seven days.')
    parser.add_argument('--bucket', help='S3 database bucket. Default: ' + s3_bucket, default=s3_bucket)
    parser.add_argument('--backup-path', help='Full path to backup directory. Default: ' + backup_dir, default=backup_dir)
    parser.add_argument('--docroot-path', help='Full path to backup directory. Default: ' + docroot_dir, default=docroot_dir)
    parser.add_argument('--dryrun', action='store_true', help="Perform a dry run. Default: %r" % dry_run, default=dry_run)
    parser.add_argument('--force-dump', action='store_true', help="Perform a database dump, even if the dump file already exists. Default: %r" % force_dump, default=force_dump)
    args = parser.parse_args()

    # Create backup settings tuple
    BackupSettings = namedtuple('BackupSettings', 's3_bucket backup_dir docroot_dir dry_run force_dump')
    settings = BackupSettings(s3_bucket = args.bucket, backup_dir = os.path.normpath(args.backup_path), docroot_dir = os.path.normpath(args.docroot_path), dry_run = args.dryrun, force_dump = args.force_dump)

    return settings;

# Initialize settings
settings = get_settings()


# Define helper functions
def get_s3_dumps():
    awscmd_db_files = 'aws s3api list-objects --bucket ' + settings.s3_bucket + ' --query Contents[].Key --output json';
    result = os.popen(awscmd_db_files).read()
    dumps = json.loads(result);
    if (dumps == None):
        dumps = []

    return dumps;

# Return a list of database dump file names for a given date range
def previous_days(start, total):
    start = date.today()
    days = []
    for i in range(total):
        day = start - timedelta(i)
        days.append(day.strftime('db_backup-%d-%m-%y.sql.gz'))

    return days;

# Return a list of database dumps located in the backup directory
def get_local_dumps():
    return glob.glob(settings.backup_dir + '/db_backup-*.sql.gz');

# Return a list of database dump file names for the past seven days
def last_seven_days():
    return previous_days(date.today(), 7);

# Delete a remote database dump file on S3.
def delete_remote_db(db):
    print "Deleting remote dump " + db + "..."
    awscmd_delete_file = 'aws s3 rm s3://' + settings.s3_bucket + '/' + db
    if (not settings.dry_run):
        print os.popen(awscmd_delete_file).read()

    print "Done.\n"
    
# Delete a local database dump file
def delete_local_db(db):
    print "Deleting local dump " + db + "..."
    if (not settings.dry_run):
        os.remove(db)

    print "Done.\n"
    
# Upload a databse dump file to S3
def upload_db(filename):
    if not os.path.isfile(filename):
        print "Database does not exist: " + filename
        return;

    print "Uploading " + filename + "..."
    if (not settings.dry_run):
        awscmd_upload_file = 'aws s3 cp ' + filename + ' s3://' + settings.s3_bucket + '/' + os.path.basename(filename)
        print os.popen(awscmd_upload_file).read()

    print "Done.\n"

# Return TRUE if the given database dump file name belongs to a given date range
def is_old_dump(db, days):
    db = os.path.basename(db)
    match = re.match( r'^(db_backup-\d{2}-\d{2}-\d{2}).*\.sql\.gz$', db)
    if (not match):
        raise RuntimeError("Dump filename \"%s\" is not properly formatted." % db);

    dump = match.group(1) + '.sql.gz'
    if (dump in days):
        return False
    else:
        return True

# Remove old database dump files
def prune_dumps(days, db_files, delete_callback):
    count = 0
    if (db_files != None):
        for db in db_files:
            if is_old_dump(db, days):
                delete_callback(db)
                count += 1

    return count

# Remove remote old database dump files stored on S3
def prune_remote_dumps(days):
    count = prune_dumps(days, get_s3_dumps(), delete_remote_db)
    print "Removed %d old dumps from S3" % count

# Remove local old database dump files
def prune_local_dumps(days):
    count = prune_dumps(days, get_local_dumps(), delete_local_db);
    print "Removed %d old local dumps" % count

# Upload new database dump files
def upload_new_dumps(days):
    count = 0
    remote_dumps = get_s3_dumps()
    for filename in get_local_dumps():
        db = os.path.basename(filename) 
        if ((len(remote_dumps) == 0 or db not in remote_dumps) and not is_old_dump(db, days)):
            upload_db(filename)
            count += 1

    print "Uploaded %d dumps to S3" % count 

# Create a database dump file for today
def create_db_dump():
    filename = settings.backup_dir + '/' + date.today().strftime('db_backup-%d-%m-%y.sql')

    # Do not create a db dump if a file already exists and the force-dump option
    # is false.
    if (os.path.isfile(filename + '.gz') and not settings.force_dump):
        print "%s already exists. Aborting DB dump." % filename
        return

    drushcmd_sql_dump = '/tmp/drush -r ' + settings.docroot_dir + ' sql-dump --result-file=' + filename + ' --structure-tables-key=common --gzip'
    print "Creating database dump %s" % filename + '.gz'
    if (not settings.dry_run):
        print os.popen(drushcmd_sql_dump).read()

    print "Done."

# Main entry point
def run():
    # Create one DB dump for today
    create_db_dump()

    # Get the xyz.sql.gz dump filename for the past seven days.
    # Format: db_backup-[day]-[month]-[year].sql.gz
    # eg. db_backup-25-08-16.sql.gz
    days = last_seven_days()
        
    # Delete old database files in S3
    prune_remote_dumps(days)

    # Delete old database files on this server
    prune_local_dumps(days)

    # Upload database to S3
    upload_new_dumps(days)

run()
