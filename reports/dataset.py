import os
import sys
import csv
import pprint
import smtplib
import mimetypes
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class DatasetReports(object):
    def __init__(self, dataverse_api=None, dataverse_database=None, config=None):
        if dataverse_api is None:
            print('Dataverse API required to create dataset reports.')
            return
        if dataverse_database is None:
            print('Dataverse database required to create dataset reports.')
            return
        if config is None:
            print('Dataverse configuration required to create dataset reports.')
            return

        self.dataverse_api = dataverse_api
        self.dataverse_database = dataverse_database

        # Ensure trailing slash on work_dir
        if config['work_dir'][len(config['work_dir'])-1] != '/':
            config['work_dir'] = config['work_dir'] + '/'

        self.config = config

        self.logger = logging.getLogger('dataverse-reports')

    def generate_reports(self, type='all'):
        if type == 'all':
            self.logger.info("Generating report of all datasets for super admin.")
            self.report_datasets_admin_recursive()
        elif type == 'institutions':
            self.logger.info("Generating report of datasets for each institution.")
            for key in self.config['accounts']:
                account_info = self.config['accounts'][key]

                self.logger.info("Generating recursive report for %s.", account_info['name'])
                self.report_datasets_recursive(account_info)

    def report_datasets_admin_recursive(self):
        # List of Datasets
        datasets = []
        report_file_paths = []

        for key in self.config['accounts']:
            account_info = self.config['accounts'][key]
            self.logger.info("Generating dataset report for %s.", account_info['identifier'])
            self.load_datasets_recursive(datasets, account_info['identifier'])

            if len(datasets) > 0:
                # Write results to CSV file
                output_file = account_info['identifier'] + '-datasets.csv'
                self.save_report(output_file_path=self.config['work_dir'] + output_file, headers=self.fieldnames, data=datasets)

                # Add file to reports list
                report_file_paths.append(self.config['work_dir'] + output_file)

            # Reset list
            datasets = []

        # Send results to admin email addresses
        if len(report_file_paths) > 0:
            self.email_report_admin(report_file_paths=report_file_paths)

    def report_datasets_recursive(self, account_info):
        # List of datasets
        datasets = []

        self.logger.info("Begin loading datasets for %s.", account_info['identifier'])
        self.load_datasets_recursive(datasets, account_info['identifier'])
        self.logger.info("Finished loading %s datasets for %s", str(len(datasets)), account_info['identifier'])

        return datasets

    def load_datasets_recursive(self, datasets={}, dataverse_identifier=None):
        if dataverse_identifier is None:
            self.logger.error("Dataverse identifier is required.")
            return

        self.logger.info("Loading dataverse: %s.", dataverse_identifier)

        # Load dataverse
        dataverse_response = self.dataverse_api.get_dataverse(identifier=dataverse_identifier)
        response_json = dataverse_response.json()
        if 'data' in response_json:
            dataverse = response_json['data']

            self.logger.info("Dataverse name: %s", dataverse['name'])

            # Retrieve dvObjects for this dataverse
            dataverse_contents = self.dataverse_api.get_dataverse_contents(identifier=dataverse_identifier)
            self.logger.info('Total dvObjects in this dataverse: ' + str(len(dataverse_contents)))
            for dvObject in dataverse_contents:
                if dvObject['type'] == 'dataset':
                    # Add dataset to this dataverse
                    self.logger.info("Adding dataset %s to dataverse %s.", str(dvObject['id']), str(dataverse_identifier))
                    self.add_dataset(datasets, dataverse_identifier, dvObject['id'])
                if dvObject['type'] == 'dataverse':
                    self.logger.info("Found new dataverse %s.", str(dvObject['id']))
                    self.load_datasets_recursive(datasets, dvObject['id'])
        else:
            self.logger.warn("Dataverse was empty.")

    def add_dataset(self, datasets, dataverse_identifier, dataset_id):
        # Load dataset
        self.logger.info("Dataset id: %s", dataset_id)
        dataset_response = self.dataverse_api.get_dataset(identifier=dataset_id)
        response_json = dataset_response.json()
        if 'data' in response_json:
            dataset = response_json['data']

            if 'latestVersion' in dataset:
                latest_version = dataset['latestVersion']
                metadata_blocks = latest_version['metadataBlocks']

                # Flatten the latest_version information
                for key, value in latest_version.items():
                    if key != 'metadataBlocks':
                        dataset[key] = value

                    # Flatten the nested citation fields information
                    citation = metadata_blocks['citation']
                    fields = citation['fields']
                    for item in fields:
                        self.logger.debug("Looking at field: %s.", item['typeName'])
                        valuesString = self.get_value_recursive('', item)
                        if valuesString.endswith(' ; '):
                            valuesString = valuesString[:-len(' ; ')]

                        typeName = item['typeName']
                        dataset[typeName] = valuesString

                # Remove nested information
                dataset.pop('latestVersion')

            # Use dataverse_database to retrieve cumulative download count of file in this dataset
            download_count = self.dataverse_database.get_download_count(dataset_id=dataset_id)
            self.logger.info("Download count for dataset: %s", str(download_count))
            dataset['downloadCount'] = download_count

            if 'files' in dataset:
                contentSize = 0
                files = dataset['files']
                for file in files:
                    if 'dataFile' in file:
                        dataFile = file['dataFile']
                        filesize = int(dataFile['filesize'])
                        contentSize += filesize
                self.logger.info('Totel size (bytes) of all files in this dataset: %s', str(contentSize))
                # Convert to megabytes for reports
                dataset['contentSize (MB)'] = (contentSize/1048576)

            # Retrieve dataverse to get alias
            dataverse_response = self.dataverse_api.get_dataverse(identifier=dataverse_identifier)
            response_json = dataverse_response.json()
            dataverse = response_json['data']

            self.logger.info("Adding dataset to dataverse with alias: %s", str(dataverse['alias']))
            dataset['dataverse'] = dataverse['alias']
            datasets.append(dataset)
        else:
            self.logger.warn("Dataset was empty.")

    def get_value_recursive(self, valuesString, field):
        if not field['multiple']:
            if field['typeClass'] == 'primitive':
                valuesString += field['value']
                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            elif field['typeClass'] == 'controlledVocabulary':
                subValue = ''
                for value in field['value']:
                    subValue += value + ', '
                subValue = subValue[:-2]
                valuesString += subValue
                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            elif field['typeClass'] == 'compound':
                subValue = ''
                if isinstance(field['value'], list):
                    for value in field['value']:
                        if isinstance(value, str):
                            self.logger.debug("Value: %s", value)
                        for key, elements in value.items():
                            if not elements['multiple']:
                                subValue += elements['value']
                            else:
                                subValue += self.get_value_recursive(valuesString, subValue, elements['value'])

                            self.logger.debug("New subValue: %s", subValue)
                            subValue += " - "

                        nick
                        valuesString += subValue + " ; "
                else:
                    value = field['value']
                    for key, elements in value.items():
                        if not elements['multiple']:
                            subValue += elements['value']
                        else:
                            subValue += self.get_value_recursive(valuesString, subValue, elements['value'])

                        self.logger.debug("New subValue: %s", subValue)
                        subValue += " - "

                    valuesString += subValue + " ; "

                if valuesString.endswith(' ; '):
                    valuesString = valuesString[:-len(' ; ')]
                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            else:
                self.logger.debug("Unrecognized typeClass: %s", field['typeClass'])
        else:
            if field['typeClass'] == 'primitive':
                subValue = ''
                for value in field['value']:
                    subValue += value + ', '
                subValue = subValue[:-2]
                valuesString += subValue
                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            elif field['typeClass'] == 'controlledVocabulary':
                subValue = ''
                for value in field['value']:
                    subValue += value + ', '
                subValue = subValue[:-2]
                valuesString += subValue
                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            elif field['typeClass'] == 'compound':
                subValue = ''
                for value in field['value']:
                    for key, elements in value.items():
                        self.logger.debug("Key: %s", key)
                        if not elements['multiple']:
                            subValue += elements['value']
                        else:
                            subValue += self.get_value_recursive(valuesString, subValue, elements['value'])

                        self.logger.debug("New subValue: %s", subValue)
                        subValue += " - "

                    subValue = subValue[:-3]
                    valuesString += subValue + " ; "

                self.logger.debug("New value of valuesString: %s", str(valuesString))
                return valuesString
            else:
                self.logger.debug("Unrecognized typeClass: %s", field['typeClass'])
