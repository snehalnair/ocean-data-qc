# -*- coding: utf-8 -*-
#########################################################################
#    License, authors, contributors and copyright information at:       #
#    AUTHORS and LICENSE files at the root folder of this application   #
#########################################################################

from bokeh.util.logconfig import bokeh_logger as lg
from ocean_data_qc.constants import *
from ocean_data_qc.data_models.exceptions import ValidationError
from ocean_data_qc.env import Environment

import json
from os import path, environ, getenv
import re
from math import *
import seawater as sw
import types
import subprocess as sbp
from importlib import import_module


class ComputedParameter(Environment):
    env = Environment

    def __init__(self, cruise_data=False):
        lg.info('-- INIT COMPUTED PARAMETER')
        self.sandbox_vars = None
        self.sandbox_funcs = None
        if cruise_data is not False:
            self.cruise_data = cruise_data
        else:
            self.cruise_data = self.env.cruise_data

        # NOTE: check octave availability again here because if we check shared_data maybe
        #       the value is not updated due to asyncronous matters
        self.equations = None
        self.import_octave_equations()

    def import_octave_equations(self):
        lg.info('>> OCTAVE PATH: {}'.format(self.env.oct_eq.oct_exe_path))
        oc_output = sbp.getstatusoutput('{} --eval "OCTAVE_VERSION"'.format(self.env.oct_eq.oct_exe_path))
        if oc_output[0] == 0:
            lg.info('>> OCTAVE DETECTED FROM PYTHON, VERSION: {}'.format(
                oc_output[1].split('=')[1].strip())
            )
            self.equations = self.env.oct_eq  # remove methods that are not equations
        else:
            lg.warning('>> OCTAVE UNDETECTED')
            self.equations = None

    @property
    def proj_settings_cps(self):
        # TODO: this is executed many time when the app load a file, avoid multiple file reading to improve efficiency
        try:
            proj_settings = json.load(open(PROJ_SETTINGS))
            return proj_settings['computed_params'] if 'computed_params' in proj_settings else {}
        except Exception:
            raise ValidationError(
                'Project JSON settings file could be opened to process the calculated parameters',
                rollback='cd'  # TODO: only if we are loading the files in the initialization
            )

    def add_computed_parameter(self, arg):
        ''' It adds the computed parameter to cols and to the project.
            Previous to this method we had to check the dependencies and
            that all the columns needed are in the current dataframe
        '''
        val = arg.get('value', False)
        prevent_save = arg.get('prevent_save', False)
        if val is False:
            return {
                'success': False,
                'msg': 'value is mandatory',
            }

        for cp in self.proj_settings_cps:  # NOTE: list of dicts, I need to iterate over all the items to get the cp to add
            if cp['param_name'] == val:
                prec = int(cp['precision'])
                new_cp = {
                    'eq': cp['equation'],
                    'computed_param_name': cp['param_name'],
                    'precision': prec,
                }
                result = self.compute_equation(new_cp)
                if result.get('success', False):
                    self.cruise_data.cols[val] = {
                        'external_name': [],
                        'data_type': 'integer' if prec == 0 else 'float',
                        'attrs': ['computed'],
                        'unit': cp.get('units', False),
                        'precision': prec,
                        'export': False
                    }
                    if prevent_save is False:
                        self.cruise_data.save_col_attribs()
                    lg.info('>> CP <<{}>> ADDED'.format(val))
                else:
                    msg = ''
                    if 'error' in result:
                        msg = result.get('error', '')  # TODO: remove "\n" fro here?
                    elif 'msg' in result:
                        msg = result.get('msg', '')
                    lg.warning('>> CP <<{}>> COULD NOT BE COMPUTED: {}'.format(
                        cp['param_name'], msg
                    ))
                return result

    def compute_equation(self, args):
        try:
            prec = int(args.get('precision', 5))
        except Exception:
            lg.error('Precision value could not be cast to integer value')
        (eq, computed_param_name, precision) = (
            args.get('eq', ''),
            args.get('computed_param_name', 'AUX'),
            prec
        )
        eq = re.sub(' ', '', eq)   # remove spaces

        if eq == '':
            lg.error('ERROR: Empty equation')

        def repl(match):
            """ This function is run for each found ocurrence """
            inner_word = match.group(0)
            new_var = False
            param_name = inner_word[2:-1]   # removin characters: ${PARAM} >> PARAM
            for elem in self.proj_settings_cps:
                if elem['param_name'] == param_name:
                    new_var = '({})'.format(elem.get('equation', False))

            if new_var is False:
                lg.error('The computed parameter does not exist')
            lg.info('>> INNER WORD: {} | NEW VAR: {}'.format(inner_word, new_var))
            return new_var

        while re.search(r'\$\{[a-zA-Z0-9_]+\}', eq) is not None:
            eq = re.sub(r'\$\{[a-zA-Z0-9_]+\}', repl, eq)

        if self.sandbox_funcs is None:
            self.sandbox_funcs = self._get_sandbox_funcs(locals())
        if self.sandbox_vars is None:
            self.sandbox_vars = self._get_sandbox_vars(globals())
        ids = self._get_eq_ids(eq)

        # check if all the identifiers are in the df
        for i in ids:
            if i not in self.cruise_data.df.columns:  # already calculated parameters also can be use as columns
                return {
                    'success': False,
                    'msg': 'Some identifiers do not exist in the current dataframe: {}'.format(i),
                }

        eq = '{} = {}'.format(computed_param_name, eq)
        # lg.info('>> EQUATION: {}'.format(eq))
        try:
            self.cruise_data.df.eval(
                expr=eq,
                engine='python',                 # NOTE: numexpr does not support custom functions
                inplace=True,
                local_dict=self.sandbox_funcs,
                global_dict=self.sandbox_vars
            )
        except Exception as e:
            # lg.warning('>> THE CP {} COULD NOT BE CALCULATED: {}'.format(computed_param_name, e))
            return {
                'success': False,
                'msg': 'The equation could not be computed: {}'.format(eq),
                'error': '{}'.format(e),
            }
        if computed_param_name == 'AUX' and 'AUX' in self.cruise_data.df.columns:
            del self.cruise_data.df['AUX']
        else:
            self.cruise_data.df = self.cruise_data.df.round({computed_param_name: precision})

        return {
            'success': True,
        }

    def _get_eq_ids(self, eq):
        ''' Return a list of identifiers used by the equation
            The parameters ${} should already be replaced before
            Pure numbers are deleted from the list
        '''
        func_list = []
        for key, value in self.sandbox_funcs.items():
            if value != None:
                func_list.append(key)

        var_list = []
        for key, value in self.sandbox_vars.items():
            if value != None:
                var_list.append(key)

        ids = re.findall('[a-zA-Z0-9_]+', eq)
        ids = [x for x in ids if (x not in func_list) and (x not in var_list)]

        # remove numbers from the list
        ids = [x for x in ids if re.match(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?', x) is None]
        return ids

    def _get_sandbox_funcs(self, loc_dict={}):
        local_dict = loc_dict.copy()        # deepcopy() > recursively  ???

        for elem in local_dict:             # resets all the values
            local_dict[elem] = None

        # math functions
        local_dict.update({
            'acos': acos, 'asin': asin, 'atan': atan, 'atan2': atan2,
            'ceil': ceil, 'cos': cos, 'cosh': cosh, 'degrees': degrees,
            'exp': exp, 'fabs': fabs, 'floor': floor, 'fmod': fmod,
            'frexp': frexp, 'hypot': hypot, 'ldexp': ldexp, 'log': log,
            'log10': log10, 'modf': modf, 'pow': pow, 'radians': radians,
            'sin': sin, 'sinh': sinh, 'sqrt': sqrt, 'tan': tan, 'tanh': tanh,
        })

        # seawater functions
        local_dict.update({
            'cndr': sw.library.cndr, 'salds': sw.library.salrp, 'salrt': sw.library.salrt, 'seck': sw.library.seck,
            'sals': sw.library.sals, 'smow': sw.library.smow, 'T68conv': sw.library.T68conv, 'T90conv': sw.library.T90conv,

            'adtg': sw.eos80.adtg, 'alpha': sw.eos80.alpha, 'aonb': sw.eos80.aonb,
            'beta': sw.eos80.beta, 'dpth': sw.eos80.dpth, 'g': sw.eos80.g, 'salt': sw.eos80.salt, 'fp': sw.eos80.fp,
            'svel': sw.eos80.svel, 'pres': sw.eos80.pres, 'dens0': sw.eos80.dens0, 'dens': sw.eos80.dens,
            'pden': sw.eos80.pden, 'cp': sw.eos80.cp, 'ptmp': sw.eos80.ptmp, 'temp': sw.eos80.temp,

            'bfrq': sw.geostrophic.bfrq, 'svan': sw.geostrophic.svan, 'gpan': sw.geostrophic.gpan, 'gvel': sw.geostrophic.gvel,

            'dist': sw.extras.dist, 'f': sw.extras.f, 'satAr': sw.extras.satAr,
            'satN2': sw.extras.satN2, 'satO2': sw.extras.satO2, 'swvel': sw.extras.swvel,
        })
        if self.equations is not None:
            for elem_str in dir(self.equations):
                if elem_str[0] != '_' and elem_str not in ['guess_oct_exe_path', 'set_oct_exe_path']:
                    elem_obj = getattr(self.equations, elem_str)
                    if isinstance(elem_obj, (\
                    types.FunctionType, types.BuiltinFunctionType,
                    types.MethodType, types.BuiltinMethodType)):
                        # lg.info('>> ACCEPTED METHOD: {}'.format(elem_str))
                        local_dict.update({elem_str: elem_obj})
        return local_dict

    def _get_sandbox_vars(self, glob_dict={}):
        global_dict = glob_dict.copy()

        for elem in global_dict:
            global_dict[elem] = None

        return global_dict

    def check_dependencies(self):
        ''' If the CP can be computed then the dependencies are satisfied
            Maybe it is a good idea to avoid the eval method for efficiency?

            This is used when the form 'add_computed_parameter_expression' is loaded

            @return = {
                'cp_param_1': True,                 # dependencies satisfied
                'cp_param_2': False,                 # dependencies don't satisfied
            }
        '''
        proj_settings = json.load(open(PROJ_SETTINGS))
        computed_params = proj_settings.get('computed_params', False)
        if computed_params is not False:
            result = {}
            for cp in computed_params:
                args = {
                    'eq': cp.get('equation', False),
                }
                ce_result = self.compute_equation(args)
                if ce_result.get('success', False) is True:
                    result.update({
                        cp.get('param_name'): True
                    })
                else:
                    result.update({
                        cp.get('param_name'): False
                    })
            return result
        else:
            return {}

    def get_all_parameters(self):
        lg.info('-- GET ALL PARAMETERS')
        cols = self.cruise_data.get_cols_by_attrs(
            ['param', 'flag', 'non_qc', 'required']
        )
        deps = self.check_dependencies()
        cp_cols = self.cruise_data.get_cols_by_attrs('computed')
        return dict(
            columns=cols,
            dependencies=deps,
            computed=cp_cols
        )

    def delete_computed_parameter(self, args):
        ''' Delete the value passed in the argument:
            args = {
                'value': 'example_column',
            }
        '''
        lg.info('-- DELETE COMPUTED PARAMETER')
        value = args.get('value', False)
        current_columns = self.cruise_data.get_cols_by_attrs(['all'])
        if value in current_columns:
            try:
                if value in self.cruise_data.df.columns:
                    del self.cruise_data.df[value]
                del self.cruise_data.cols[value]
                return {
                    'success': True,
                }
            except Exception:
                return {
                    'success': False,
                }
        else:
            return {
                'success': False,
            }


