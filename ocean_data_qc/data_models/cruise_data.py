# -*- coding: utf-8 -*-
#########################################################################
#    License, authors, contributors and copyright information at:       #
#    AUTHORS and LICENSE files at the root folder of this application   #
#########################################################################

from bokeh.util.logconfig import bokeh_logger as lg
from ocean_data_qc.constants import *
from ocean_data_qc.data_models.exceptions import ValidationError
from ocean_data_qc.data_models.computed_parameter import ComputedParameter
from ocean_data_qc.data_models.cruise_data_export import CruiseDataExport

import csv
import json
import os
import pandas as pd
import numpy as np
from os import path
import hashlib
from datetime import datetime
from shutil import rmtree
import re


class CruiseData(CruiseDataExport):
    ''' This class is gathering all the common methods needed to manage
        the aqc, csv and whp files (instantiated with the children classes)
    '''
    env = CruiseDataExport.env

    def __init__(self, original_type='', cd_aux=False):
        lg.info('-- INIT CRUISE DATA PARENT')
        self.original_type = original_type        # original.csv type (whp, csv)
        self.cd_aux = cd_aux
        self.df = None                            # numeric DataFrame
        self.df_str = None                        # string DataFrame
        self.moves = None
        self.cols = {}
        self.col_mappings = {}                   # to set in external_name

        self._validate_original_data()
        self._set_moves()                         # TODO: this is not needed for cd_update
        self._set_df()
        self._rmv_empty_columns()
        self._prep_df_columns()
        self.cp_param = ComputedParameter(self)

    def _rmv_empty_columns(self):
        lg.info('-- REMOVE EMPTY COLUMNS (all values with -999)')
        cols_to_rmv = []
        flags_to_rmv = []
        basic_params = self.env.f_handler.get_custom_cols_by_attr('basic_param')
        for col in self.df:
            if col not in basic_params:  # empty basic param columns are needed for some calculated params
                if self.df[col].str.contains(NA_REGEX).all():
                    cols_to_rmv.append(col)
                    if f'{col}_FLAG_W' in self.df:
                        flags_to_rmv.append(f'{col}_FLAG_W')
        if len(cols_to_rmv) > 0:
            lg.warning(f'>> THE FOLLOWING COLUMNS WERE REMOVED DUE TO -999: {",".join(cols_to_rmv)}')
            self.add_moves_element(
                'cols_removed',
                f'{",".join(cols_to_rmv)} param columns were removed'
            )
        if len(flags_to_rmv):
            lg.warning(f'>> THE FOLLOWING COLUMNS FLAGS WERE REMOVED DUE TO -999: {",".join(flags_to_rmv)}')
            self.add_moves_element(
                'flags_cols_removed',
                f'{",".join(flags_to_rmv)} flag columns were removed'
            )
        cols_to_rmv.extend(flags_to_rmv)
        self.df = self.df.drop(columns=cols_to_rmv)

    def _set_cols_from_scratch(self):
        """ The main attributes of the object are filled:

                "cols": {
                    "ALKALI": {
                        "external_name": "alkali",          # in custom_settings is a list
                        "attrs": ["param", "required", ],
                        "data_type": "float",
                        "unit": "UMOL/KG",
                        "precision": 3,
                        "export": True
                    },
                    "ALKALI_FLAG_W": {
                        "external_name": False,
                        "attrs": ["param_flag", "qc_param_flag"],
                        "data_type": "integer"
                        "unit": False,  # >> False
                        "precision": 0,
                        "export": True
                    }
                }
        """
        lg.info('-- SET COLS ATTRIBUTES FROM SCRATCH')
        pos = 0
        column_list = self.df.columns.tolist()
        units_list = self._check_unit_row()

        if len(units_list) > 0:
            for column in column_list:
                self._add_column(column=column)
                if str(units_list[pos]) == 'nan':
                    self.cols[column]['unit'] = False
                else:
                    self.cols[column]['unit'] = units_list[pos]
                pos += 1
        else:
            for column in column_list:
                self._add_column(column=column)

        for key, value in self.col_mappings.items():
            # self.col_mappings = {
            #     'external_value': new_value,  >> the new_value is the used inside the app
            # }
            self.cols[value]['external_name'] = key

        # lg.info(json.dumps(self.cols, sort_keys=True, indent=4))

    def _check_unit_row(self):
        ''' Checks if the file has an units row or not.
            The df should have all strings and nan values
                * if there is at least one nan in the row               > unit row
                * if all the cells are strings                          > unit row
                * if there is at least one number (stored as string)    > no unit row
        '''
        lg.info('-- CHECK IF THE UNIT ROW EXISTS')
        exp = re.compile("^-?\d+?(\.\d+)?$")
        def is_number(s):
            ''' Returns True if the string is a number:
                    float or integer
            '''
            if exp.match(s) is None:
                return s.isdigit()  # to check if all are digits
            return True

        units = self.df.iloc[0].values.tolist()
        no_unit_row = False
        for u in units:  # the loop continues only if it is a string and not number
            if not isinstance(u, str) and np.isnan(u):
                break
            if is_number(u):
                no_unit_row = True
                break
        if no_unit_row is False:
            self.df = self.df[1:-1].reset_index(drop=True)  # rewrite index column and remove the units row
            return units
        else:
            return []

    def _validate_flag_values(self):
        ''' Assign 9 to the rows where the param has an NaN
            Also checks if there is any NaN or incorrect value in the flag columns
        '''
        lg.info('-- VALIDATE FLAG VALUES')
        for param in self.df:
            flag = param + FLAG_END
            if flag in self.df:
                upds = self.df[self.df[param].isnull() & (self.df[flag] != 9)].index.tolist()
                if len(upds) > 0:
                    empty_rows = {}
                    empty_rows[flag] = 9
                    self.df[self.df[param].isnull() & (self.df[flag] != 9)] = self.df[self.df[param].isnull() & (self.df[flag] != 9)].assign(**empty_rows)
                    self.add_moves_element(
                        'flag_column_updated',
                        f'The flag column {flag} had some NaN values in the related parameter column. '
                        f'It was set to the empty default value 9 in {len(upds)} rows.'
                    )

                # NOTE: if the flag value is NaN or is not between [0-9] > throw error or reset to 9?
                if self.df[flag].isnull().any():
                    raise ValidationError(
                        'The flag column {} has a/some null value/s in the row/s '
                        '(row position taking into account just the data cells): {}'.format(
                            flag, str(self.df[self.df[flag].isnull()].index.tolist())[1:-1]
                        ),
                        rollback=self.rollback
                    )
                if self.df[(self.df[flag] > 9) | (self.df[flag] < 0)].index.any():
                    raise ValidationError(
                        'The flag column {} must have values between 0-9 in the row '
                        '(row position taking into account just the data cells): {}'.format(
                            flag, str(self.df[(self.df[flag] > 9) | (self.df[flag] < 0)].index.tolist())[1:-1]
                        ),
                        rollback=self.rollback
                    )

    def _add_column(self, column='', units=False, export=True):
        ''' Adds a column to the self.cols dictionary
            This dictionary is useful to select some columns by type
                * required      - required columns
                * param         - parameter columns
                * param_flag    - flag columns
                * qc_param_flag - flag columns created by this app
                * non_qc_param  - parameters without flag columns associated
                * computed      - computed parameters
                * created       - if the column was created by the app

            TODO: add all arguments or add a param as a dictionary with all the attributes
                  this method also should work if something should be modified or removed?
        '''
        if column not in self.get_cols_by_attrs('all'):
            self.cols[column] = {
                'external_name': False,
                'attrs': [],
                'unit': units,
                'precision': False,
                'export': export
            }
            non_qc_params = self.env.f_handler.get_custom_cols_by_attr('non_qc_param')
            if column.endswith(FLAG_END):
                self.cols[column]['attrs'] += ['param_flag']
                flags_not_to_qc = [x + FLAG_END for x in non_qc_params]
                if column not in flags_not_to_qc:
                    self.cols[column]['attrs'] += ['qc_param_flag']
            else:
                basic_params = self.env.f_handler.get_custom_cols_by_attr('basic_param')
                if column in basic_params:
                    self.cols[column]['attrs'] += ['basic_param']
                required_cols = self.env.f_handler.get_custom_cols_by_attr('required')
                if column in required_cols:
                    self.cols[column]['attrs'] += ['required']
                elif column in non_qc_params:
                    self.cols[column]['attrs'] += ['non_qc_param']
                else:
                    self.cols[column]['attrs'] += ['param']
                self.create_missing_flag_col(column)
        else:
            lg.warning('>> THE COLUMN ALREADY EXISTS AND IT CANNOT BE CREATED AGAIN')

    def create_missing_flag_col(self, param=None):
        ''' Make sure there is a flag column for each param parameter '''
        if param is not None and isinstance(param, str) and not param.endswith(FLAG_END):
            flag = param + FLAG_END
            non_qc_params = self.env.f_handler.get_custom_cols_by_attr('non_qc_param')
            if flag not in self.df and param not in non_qc_params:
                lg.info('>> CREATING FLAG COLUMN: {}'.format(flag))
                values = ['2'] * len(self.df.index)
                self.df[flag] = values
                self.cols[flag] = {
                    'external_name': False,
                    'attrs': ['param_flag', 'qc_param_flag', 'created'],
                    'unit': False,
                    'export': True
                }
                self.add_moves_element(
                    'flag_column_added',
                    'Flag column that was missing added to the project '
                    'with default value "2" in all the rows: {}'.format(flag)
                )

    def _init_basic_params(self):
        ''' Initializates the dataframe with the basic params that all csv files should have
            to compute some calculated parameters. If some of them do not exist in the dataframe yet,
            they are created with NaN values.
        '''
        basic_list = self.env.f_handler.get_custom_cols_by_attr('basic_param')
        for c in basic_list:
            all_cols = self.get_cols_by_attrs('all')
            if c not in all_cols:
                self.df[c] = np.array([np.nan] * self.df.index.size)
                self.add_moves_element(
                    'column_added',
                    'Basic column added to the project'
                    ' with default value "NaN" in all the rows: {}'.format(c)
                )
                # NOTE: I don't call to _add_column because I don't want to create the flag column
                self.cols[c] = {
                    'external_name': False,
                    'attrs': ['param', 'basic_param', 'created', ],
                    'unit': False,
                    'precision': False,
                    'export': False
                }

    def get_cols_from_settings_file(self):
        """ The columns are set directly from the settings.json file """
        self.cols = self.env.f_handler.get('columns', path.join(TMP, 'settings.json'))

    def get_cols_by_attrs(self, column_attrs=[], discard_nan=False):
        ''' Possible attrs:
                * computed      - calculated parameters
                * param         - parameters
                * non_qc_param  - params without qc column
                * param_flag    - existing flags for the params that were loaded from the beginning
                * qc_param_flag - flags that were created by the application with value 2
                * required      - required columns

            @discard_nan - discards columns with all the values = NaN

            NOTE: a flag param could have the attrs 'param_flag' and 'qc_param_flag' at the same time

            TODO: use self.cols for this instance, and get custom_cols if needed (basic_params) ??
        '''
        if isinstance(column_attrs, str):
            column_attrs = [column_attrs]
        if len(column_attrs) == 1 and 'all' in column_attrs:
            column_attrs = [
                'computed', 'param', 'non_qc_param',
                'param_flag', 'qc_param_flag', 'required',
                'created'
            ]
        res = []
        for t in column_attrs:
            for c in self.cols:
                if t in self.cols[c]['attrs']:
                    if c not in res:
                        res.append(c)
        res = list(set(res))  # one column may have multiple attrs
        df_cols = list(self.df.columns)
        col_positions = dict(
            [(df_cols[df_cols.index(x)], df_cols.index(x)) for x in df_cols]  # {'COL1': 0, 'COL2': 1, ...}
        )
        try:
            prepaired_list = [(col_positions[x], x) for x in res]
        except Exception:
            raise ValidationError(
                'Some columns in the settings.json file or '
                'self.cols object is not in the DataFrame'
            )
        sorted_list = sorted(prepaired_list, key=lambda elem: elem[0])  # reordering
        final_list = [x[1] for x in sorted_list]
        if discard_nan:
            final_list = self._discard_nan_columns(final_list)
        return final_list

    def _discard_nan_columns(self, col_list):
        final_cols = list(col_list)
        for c in col_list:
            if self.df[c].isnull().all():
                final_cols.remove(c)
        final_cols.sort()
        return final_cols

    @property
    def stations(self):
        return list(self.df.drop_duplicates(STNNBR)[STNNBR])

    def get_units(self, cols):
        return [self.cols[x]['unit'] for x in cols]

    def is_flag(self, flag):
        if flag[-7:] == FLAG_END and flag in self.get_cols_by_attrs(['param_flag', 'qc_param_flag']):
            return True
        else:
            return False

    def _set_df(self):
        """ it creates the self.df dataframe object
            taking into account if data.csv is created or not
        """
        lg.info('-- SET DF')
        try:
            delimiter=self.dialect.delimiter
        except:
            delimiter=','
        try:
            self.df = pd.read_csv(
                filepath_or_buffer=self.filepath_or_buffer,
                comment='#',
                delimiter=delimiter,
                skip_blank_lines=True,
                skipinitialspace=True,
                engine='c',                 # engine='python' is more versatile, 'c' is faster
                dtype=str,                  # useful to make some replacements before casting to numeric values
                skiprows=self.skiprows
                # verbose=False             # indicates the number of NA values placed in non-numeric columns
            )
            lg.info('>> PANDAS using \'c\' engine')
        except:
            self.df = pd.read_csv(
                filepath_or_buffer=self.filepath_or_buffer,
                comment='#',
                delimiter=delimiter,
                skip_blank_lines=True,
                skipinitialspace=True,
                engine='python',
                dtype=str,
                skiprows=self.skiprows
                # verbose=False
            )
            lg.info('>> PANDAS using \'python\' engine')
        # lg.info('\n\n>> DF: \n\n{}'.format(self.df))

    def _prep_df_columns(self):
        self.df.replace(r'\s', '', regex=True, inplace=True)  # cleans spaces: \r and \n are managed by read_csv
        non_sanitized = self.df.columns.tolist()
        self.df.columns = self._sanitize_cols(self.df.columns.tolist())  # remove spaces from columns
        self.df.columns = self._map_col_names(self.df.columns.tolist(), non_sanitized)
        self._create_btlnbr_or_sampno_column()  # >> basic params?
        self._create_date_column()

    def _create_btlnbr_or_sampno_column(self):
        if 'BTLNBR' in self.df and not 'SAMPNO' in self.df:
            self.df['SAMPNO'] = self.df['BTLNBR']
            self.add_moves_element(
                'sampno_column_added',
                'SAMPNO column was automatically generated from the column BTLNBR'
            )
        elif not 'BTLNBR' in self.df and 'SAMPNO' in self.df:
            self.df['BTLNBR'] = self.df['SAMPNO']
            self.add_moves_element(
                'sampno_column_added',
                'BTLNBR column was automatically generated from the column SAMPNO'
            )
        elif not 'BTLNBR' in self.df and not 'SAMPNO' in self.df:
            self.df['BTLNBR'] = range(self.df.index.size)
            self.df['SAMPNO'] = range(self.df.index.size)
            self.add_moves_element(
                'sampno_btlnbr_columns_added',
                'BTLNBR, SAMPNO column was automatically generated from the column '
            )

    def _create_date_column(self):
        # TODO: check what happens with this columns in the cd_update and self.env.cols

        cols = self.df.columns.tolist()
        if 'DATE' not in cols:
            lg.info('-- CREATE DATE COLUMN')
            if 'YEAR' in cols and 'MONTH' in cols and 'DAY' in cols:
                try:
                    self.df = self.df.assign(
                        DATE=pd.to_datetime(self.df[['YEAR', 'MONTH', 'DAY']]).dt.strftime('%Y%m%d')
                    )
                except Exception as e:
                    raise ValidationError(
                        'DATE column, which is a required field, does not exist. Also, it could not be created'
                        ' from YEAR, MONTH and DAY columns possibly because some of the rows do not have any value.',
                        rollback=self.rollback
                    )
                self.add_moves_element(
                    'required_column_added',
                    'DATE column was automatically generated from the columns YEAR, MONTH and DAY'
                )
            elif 'DATE_YY' in cols and 'DATE_MM' in cols and 'DATE_DD' in cols:
                mapper = dict(zip(
                    ('DATE_YY', 'DATE_MM', 'DATE_DD'),
                    ('YEAR', 'MONTH', 'DAY')
                ))
                try:
                    self.df = self.df.assign(
                        DATE=pd.to_datetime(
                            self.df[['DATE_YY','DATE_MM','DATE_DD']].rename(columns=mapper)
                        ).dt.strftime('%Y%m%d')
                    )
                except Exception as e:
                    raise ValidationError(
                        'DATE column, which is a required field, does not exist. Also, it could not be created'
                        ' from DATE_YY, DATE_MM and DATE_DD columns possibly because some of the rows do not have any value.',
                        rollback=self.rollback
                    )
                self.add_moves_element(
                    'required_column_added',
                    'DATE column was automatically generated from the columns DATE_YY, DATE_MM and DATE_DD'
                )
            else:
                raise ValidationError(
                    'DATE column, which is a required field, does not exist. Also, it could not be built'
                    ' with other columns (usually year, month and day).',
                    rollback=self.rollback
                )

    def _set_moves(self):
        """ create the self.moves dataframe object
            taking into account if moves.csv is already created or not
        """
        if path.isfile(MOVES_CSV) and os.stat(MOVES_CSV).st_size != 0:
            self.moves = pd.read_csv(
                MOVES_CSV, delimiter=',', skip_blank_lines=True,
                verbose=True, engine='python', index_col=0, dtype=str
            )
        else:
            columns = [
                'date', 'action', 'stnnbr', 'castno',
                'btlnbr', 'latitude', 'longitude', 'param', 'value', 'description'
            ]
            self.moves = pd.DataFrame(columns=columns, dtype=str)

    def _set_hash_ids(self):
        """ Create a column id for the whp-exchange files
            this new column is a hash of these fields combined:
                * STNNBR     station number
                * CASTNO     cast number (it may exist or not)
                * BTLNBR     bottle number (it may exist or not)
                * LATITUDE   latitude
                * LONGITUDE  longitude
        """
        self.df['HASH_ID'] = self.df[[
            'STNNBR', 'CASTNO', 'BTLNBR', 'LATITUDE', 'LONGITUDE'   # if BTLNBR is NaN the hash is made correctly as well
        ]].astype(str).apply(                                       # astype is 4x slower than apply
            lambda x: hashlib.sha256(str.encode(str(tuple(x)))).hexdigest(), axis=1
        )
        self.df = self.df.set_index(['HASH_ID'])

    def _validate_required_columns(self):
        lg.info('-- VALIDATE REQUIRED COLUMNS')
        required_columns = self.env.f_handler.get_custom_cols_by_attr('required')
        if (not set(self.get_cols_by_attrs('all')).issuperset(required_columns)):
            missing_columns = ', '.join(list(set(required_columns) - set(self.get_cols_by_attrs('all'))))
            raise ValidationError(
                'Missing required columns in the file: [{}]'.format(missing_columns),
                rollback=self.rollback
            )

    def _sanitize_cols(self, names):
        result = []
        for name in names:
            n = name.replace('-', '_')
            n = n.replace('+', '_')
            n = n.replace(' ', '')  # TODO: any space, not only space >> trim?
            n = re.sub(r'\s', '', n)
            n = n.upper()
            result.append(n)
            if name != n:
                self.col_mappings[name] = n
        return result

    def _map_col_names(self, sanitized, non_sanitized):
        lg.info('-- MAP COL NAMES')
        # map column names from custom settings
        custom_cols = self.env.f_handler.get('columns', CUSTOM_SETTINGS)
        for c in custom_cols.keys():
            if len(custom_cols[c]['external_name']) > 0:
                for n in custom_cols[c]['external_name']:
                    if n in sanitized and c not in sanitized:
                        sanitized[sanitized.index(n)] = c
                        self.col_mappings[sanitized.index(n)] = c

        # sometimes flags end with F instead of _FLAG_W
        for n in sanitized:
            if n + 'F' in sanitized and n + FLAG_END not in sanitized:
                key = non_sanitized[sanitized.index(f'{n}F')]
                sanitized[sanitized.index(f'{n}F')] = n + FLAG_END
                self.col_mappings[f'{key}'] = n + FLAG_END
        return sanitized

    def _replace_nan_values(self):
        ''' Replaces the -990.0, -999.00, etc values to NaN.
            There will be strings and floats in the same column because NaN is considered a float64
            and this step should be before the numeric conversion
        '''
        lg.info('-- REPLACE MISSING VALUES (-999 >> NaN)')
        self.df_str = self.df.copy(deep=True)    # TODO: this has to be synchronized when seld.df is updated
                                                 #       or should not be used anymore
        self.df.replace(
            to_replace=NA_REGEX_LIST,
            value='', #np.nan,
            inplace=True,
            regex=True,
        )

    def _convert_data_to_number(self):
        ''' Converts the DF from string to numeric values
            downcasting the resulting data to the smallest numerical dtype possible (int8 is the minimum)

            If the column has float values, all the column will have
            the same number of decimals (the maximum, zero in the right side is not taking into account)

            If a cell of a column with dtype=np.int8 is assigned to some int64 value, then the column
            is completely converted to int64
        '''
        self.df = self.df.apply(lambda x: pd.to_numeric(x, errors='ignore', downcast='integer'))
        float_prec_dict = self._set_col_precisions()

        # NOTE: Round each column by the original number of decimal places, if the value is shown somewhere
        #       or the float comparison, made in cruise_data_update.py, will work better
        self.df = self.df.round(float_prec_dict)

    def _set_col_precisions(self):
        ''' Set the precision of all the columns in self.cols['precision']
                * get the columns with float values > precision = X
                * get the columns with int values   > precision = 0
                * get the columns with str values   > precision = False
        '''
        lg.info('-- SET COL PRECISIONS')
        pd_precision = 0
        float_prec_dict = {}
        for c in self.df.select_dtypes(include=['float64']):
            if not self.df[c].isnull().all():
                df_tmp = self.df_str[c].str.contains(pat='\.', na=False)  # fills na values with False
                df_tmp = self.df_str[c][df_tmp]

                if df_tmp.index.size == 0:  # are all integer and NaN mixed
                    self.cols[c]['precision'] = 0
                    self.cols[c]['data_type'] = 'integer'
                    continue

                p = int(df_tmp.str.rsplit(pat='.', n=1, expand=True)[1].str.len().max())  # always has one '.'
                if p > pd_precision:
                    pd_precision = p
                float_prec_dict[c] = p
                self.cols[c]['precision'] = p
                self.cols[c]['data_type'] = 'float'
            else:  # empty column
                self.cols[c]['precision'] = False
                self.cols[c]['data_type'] = 'empty'

        for c in self.df.select_dtypes(include=['int8', 'int16', 'int32', 'int64']):
            self.cols[c]['precision'] = 0
            if c == 'NITRIT_FLAG_W':
                lg.warning(f'DF["NITRIT_FLAG_W"]: {self.df["NITRIT_FLAG_W"]}')
            if self.df[self.df[c] == 9][c].index.size == self.df.index.size:
                self.cols[c]['data_type'] = 'empty'
                self.cols[c]['export'] = False
            else:
                self.cols[c]['data_type'] = 'integer'

        for c in self.df.select_dtypes(include=['object']):  # or exclude=['int8', 'int16', 'int32', 'int64', 'float64']
            self.cols[c]['precision'] = False
            self.cols[c]['data_type'] = 'string'

        if pd_precision > 15:
            pd_precision = 15
        pd.set_option('precision', pd_precision)
        return float_prec_dict

    def update_flag_values(self, column, new_flag_value, row_indices):
        """ This method is executed mainly when a flag is pressed to update the values
                * column: it is the column to update, only one column
                * new_flag_value: it is the flag value
        """
        lg.info('-- UPDATE DATA --')

        lg.info('>> COLUMN: %s | VALUE: %s | ROWS: %s' % (column, new_flag_value, row_indices))
        # lg.info('\n\nData previous changed: \n\n%s' % self.df[[ column ]].iloc[row_indices])

        hash_index_list = self.df.index[row_indices]
        self.df.loc[hash_index_list,(column)] = new_flag_value

        # lg.info('\n\nData after changed: \n\n%s' % self.df[[ column ]].iloc[row_indices])

        # Update the action log
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        action = 'QC Update'

        for row in row_indices:
            stnnbr = self.df[[ 'STNNBR' ]].iloc[row][0]
            castno = self.df[[ 'CASTNO' ]].iloc[row][0]
            btlnbr = self.df[[ 'BTLNBR' ]].iloc[row][0]
            latitude = self.df[[ 'LATITUDE' ]].iloc[row][0]
            longitude = self.df[[ 'LONGITUDE' ]].iloc[row][0]
            description = '{COLUMN} flag was updated to {FLAG}, in [station {STNNBR}, cast number {CASTNO}, bottle {BTLNBR}, latitude {LATITUDE}, longitude {LONGITUDE}]'.format(
                COLUMN=column, FLAG=new_flag_value, STNNBR=stnnbr, CASTNO=castno,
                BTLNBR=btlnbr, LATITUDE=latitude, LONGITUDE=longitude,
            )
            lg.info('>> MOVES LOG: {}, {}, {}'.format(date, action, description))

            fields = [date, action, stnnbr, castno, btlnbr, latitude, longitude, column, new_flag_value, description]
            if not self.moves.empty:
                last_pos = self.moves.tail(1).index[0]
                self.moves.loc[last_pos + 1] = fields  # fastest way to add a row at the end
            else:
                self.moves.loc[0] = fields

        self.save_tmp_data()

    def add_moves_element(self, action, description):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self.moves.empty:
            last_pos = self.moves.tail(1).index[0]
            self.moves.loc[last_pos + 1] = [date, action, '', '', '', '', '', '', '', description]  # fastest way to add a row at the end
        else:
            self.moves.loc[0] = [date, action, '', '', '', '', '', '', '', description]

    def recompute_cps(self):
        ''' Compute all the calculated parameters again. Mainly after a cruise data update

            NOTE: what should happen if some column cannot be computed?
                  - Check if it is plotted in order to remove the plots?
                  - Show a error message (now only a warning appears)
        '''
        lg.info('-- RECOMPUTE CP PARAMETERS')
        cp_params = self.env.cruise_data.get_cols_by_attrs('computed')
        for c in cp_params:
            del self.cols[c]
        cps_to_rmv = []
        for c in self.cp_param.proj_settings_cps:
            if c['param_name'] not in self.cols:  # exclude the computed parameters
                res = self.cp_param.add_computed_parameter({
                    'value': c['param_name'],
                    'prevent_save': True
                })
                if res.get('success', False) is False:
                    if c['param_name'] in self.env.cur_plotted_cols:
                        cps_to_rmv.append(c['param_name'])
        if cps_to_rmv != []:
            self.env.f_handler.remove_cols_from_qc_plot_tabs(cps_to_rmv)
        self.env.cruise_data.save_col_attribs()
