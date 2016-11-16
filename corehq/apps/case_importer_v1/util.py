import json
from collections import defaultdict, namedtuple
from datetime import date

import xlrd
from django.utils.translation import ugettext_lazy as _
from couchdbkit import NoResultFound

from corehq.apps.case_importer_v1.const import LookupErrors, ImportErrors
from corehq.apps.groups.models import Group
from corehq.apps.case_importer_v1.exceptions import (
    ImporterExcelFileEncrypted,
    ImporterExcelError,
    ImporterFileNotFound,
    ImporterRefError,
    InvalidCustomFieldNameException,
    ImporterError)
from corehq.apps.users.cases import get_wrapped_owner
from corehq.apps.users.models import CouchUser
from corehq.apps.users.util import format_username
from corehq.apps.locations.models import SQLLocation, Location
from corehq.form_processor.exceptions import CaseNotFound
from corehq.form_processor.interfaces.dbaccessors import CaseAccessors
from corehq.form_processor.utils.general import should_use_sql_backend
from corehq.util.soft_assert import soft_assert
from couchexport.export import SCALAR_NEVER_WAS


# Don't allow users to change the case type by accident using a custom field. But do allow users to change
# owner_id, external_id, etc. (See also custom_data_fields.models.RESERVED_WORDS)
RESERVED_FIELDS = ('type',)
EXTERNAL_ID = 'external_id'


class ImporterConfig(namedtuple('ImporterConfig', [
    'couch_user_id',
    'excel_fields',
    'case_fields',
    'custom_fields',
    'search_column',
    'key_column',
    'value_column',
    'named_columns',
    'case_type',
    'search_field',
    'create_new_cases',
])):
    """
    Class for storing config values from the POST in a format that can
    be pickled and passed to celery tasks.
    """

    def to_dict(self):
        return self._asdict()

    def to_json(self):
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, json_dict):
        return cls(**json_dict)

    @classmethod
    def from_json(cls, json_rep):
        return cls.from_dict(json.loads(json_rep))

    @classmethod
    def from_request(cls, request):
        return cls(
            couch_user_id=request.couch_user._id,
            excel_fields=request.POST.getlist('excel_field[]'),
            case_fields=request.POST.getlist('case_field[]'),
            custom_fields=request.POST.getlist('custom_field[]'),
            search_column=request.POST['search_column'],
            key_column=request.POST['key_column'],
            value_column=request.POST['value_column'],
            named_columns=request.POST['named_columns'] == 'True',
            case_type=request.POST['case_type'],
            search_field=request.POST['search_field'],
            create_new_cases=request.POST['create_new_cases'] == 'True',
        )


def open_workbook(filename=None, **kwargs):
    _soft_assert = soft_assert(notify_admins=True)
    try:
        return xlrd.open_workbook(filename=filename, **kwargs)
    except xlrd.XLRDError as e:
        message = unicode(e)
        if message == u'Workbook is encrypted':
            raise ImporterExcelFileEncrypted(message)
        else:
            _soft_assert(False, 'displaying XLRDError directly to user', e)
            raise ImporterExcelError(message)
    except IOError as e:
        raise ImporterFileNotFound(unicode(e))
    except Exception as e:
        _soft_assert(False, 'xlrd.open_workbook raised unaccounted for error', e)
        raise ImporterFileNotFound(unicode(e))


class ExcelFile(object):
    """
    Class to deal with Excel files.

    xlrd support for .xlsx isn't complete
    NOTE: other code makes the assumption that this is the only supported
    extension so if you fix this you should also fix these assumptions
    (see get_spreadsheet)
    """

    ALLOWED_EXTENSIONS = ['xls']

    file_path = ''
    workbook = None
    column_headers = False

    def __init__(self, file_path, column_headers):
        """
        raises ImporterError or one of its subtypes
        if there's a problem with the excel file

        """
        self.file_path = file_path
        self.column_headers = column_headers

        self.workbook = open_workbook(self.file_path)
        if not self.workbook:
            raise AssertionError("open_workbook failed to return a Book object")

    def _col_values(self, sheet, index):
        return [self._fmt_value(cell) for cell in sheet.col(index)]

    def _row_values(self, sheet, index):
        return [self._fmt_value(cell) for cell in sheet.row(index)]

    def _fmt_value(self, cell):
        if cell.ctype == xlrd.XL_CELL_NUMBER:
            if int(cell.value) == cell.value:
                # Explicitly cast integers, since xlrd treats all numbers as floats
                return int(cell.value)
            else:
                return cell.value
        elif cell.ctype == xlrd.XL_CELL_DATE:
            return date(*xlrd.xldate_as_tuple(cell.value, self.workbook.datemode)[:3])
        else:
            return cell.value

    def get_first_sheet(self):
        return self.workbook.sheet_by_index(0)

    def get_header_columns(self):
        sheet = self.get_first_sheet()

        if sheet.ncols > 0:
            columns = []

            # get columns
            if self.column_headers:
                columns = self._row_values(sheet, 0)
            else:
                for colnum in range(sheet.ncols):
                    columns.append("Column %i" % (colnum,))

            return columns
        else:
            return []

    def _get_column_values(self, column_index):
        sheet = self.get_first_sheet()

        if self.column_headers:
            return self._col_values(sheet, column_index)[1:]
        else:
            return self._col_values(sheet, column_index)

    def get_unique_column_values(self, column_index):
        return list(set(self._get_column_values(column_index)))

    def _get_num_rows(self):
        sheet = self.get_first_sheet()
        return sheet.nrows

    def _get_row(self, index):
        sheet = self.get_first_sheet()
        return self._row_values(sheet, index)

    @property
    def max_row(self):
        return self._get_num_rows()

    def iter_rows(self):
        row_count = self.max_row
        for i in range(row_count):
            yield self._get_row(i)


def convert_custom_fields_to_struct(config):
    excel_fields = config.excel_fields
    case_fields = config.case_fields
    custom_fields = config.custom_fields

    field_map = {}
    for i, field in enumerate(excel_fields):
        if field:
            field_map[field] = {}

            if case_fields[i]:
                field_map[field]['field_name'] = case_fields[i]
            elif custom_fields[i]:
                # if we have configured this field for external_id populate external_id instead
                # of the default property name from the column
                if config.search_field == EXTERNAL_ID and field == config.search_column:
                    field_map[field]['field_name'] = EXTERNAL_ID
                else:
                    field_map[field]['field_name'] = custom_fields[i]
    # hack: make sure the external_id column ends up in the field_map if the user
    # didn't explicitly put it there
    if config.search_column not in field_map and config.search_field == EXTERNAL_ID:
        field_map[config.search_column] = {
            'field_name': EXTERNAL_ID
        }
    return field_map


class ImportErrorDetail(object):

    ERROR_MSG = {
        ImportErrors.InvalidOwnerId: _(
            "Owner ID was used in the mapping but there were errors when "
            "uploading because of these values. Make sure the values in this "
            "column are ID's for users or case sharing groups or locations."
        ),
        ImportErrors.InvalidOwnerName: _(
            "Owner name was used in the mapping but there were errors when "
            "uploading because of these values."
        ),
        ImportErrors.InvalidDate: _(
            "Date fields were specified that caused an error during "
            "conversion. This is likely caused by a value from excel having "
            "the wrong type or not being formatted properly."
        ),
        ImportErrors.BlankExternalId: _(
            "Blank external ids were found in these rows causing as error "
            "when importing cases."
        ),
        ImportErrors.CaseGeneration: _(
            "These rows failed to generate cases for unknown reasons"
        ),
        ImportErrors.InvalidParentId: _(
            "An invalid or unknown parent case was specified for the "
            "uploaded case."
        ),
        ImportErrors.DuplicateLocationName: _(
            "Owner ID was used in the mapping, but there were errors when "
            "uploading because of these values. There are multiple locations "
            "with this same name, try using site-code instead."
        ),
        ImportErrors.InvalidInteger: _(
            "Integer values were specified, but the values in excel were not "
            "all integers"
        ),
        ImportErrors.ImportErrorMessage: _(
            "Problems in importing cases. Please check the excel file."
        )
    }

    def __init__(self, *args, **kwargs):
        self.errors = defaultdict(dict)

    def add(self, error, row_number, column_name=None):
        self.errors[error].setdefault(column_name, {})
        self.errors[error][column_name]['error'] = _(error)

        try:
            self.errors[error][column_name]['description'] = self.ERROR_MSG[error]
        except KeyError:
            self.errors[error][column_name]['description'] = self.ERROR_MSG[ImportErrors.CaseGeneration]

        if 'rows' not in self.errors[error][column_name]:
            self.errors[error][column_name]['rows'] = []

        self.errors[error][column_name]['rows'].append(row_number)

    def as_dict(self):
        return dict(self.errors)


def convert_field_value(value):
    # coerce to string unless it's a unicode string then we want that
    if isinstance(value, unicode):
        return value
    else:
        return str(value)


def parse_search_id(config, columns, row):
    """ Find and convert the search id in an excel row """

    # Find index of user specified search column
    search_column = config.search_column
    search_column_index = columns.index(search_column)

    search_id = row[search_column_index]
    try:
        # if the spreadsheet gives a number, strip any decimals off
        # float(x) is more lenient in conversion from string so both
        # are used
        search_id = int(float(search_id))
    except ValueError:
        # if it's not a number that's okay too
        pass

    return convert_field_value(search_id)


def get_key_column_index(config, columns):
    key_column = config.key_column
    try:
        key_column_index = columns.index(key_column)
    except ValueError:
        key_column_index = False

    return key_column_index


def get_value_column_index(config, columns):
    value_column = config.value_column
    try:
        value_column_index = columns.index(value_column)
    except ValueError:
        value_column_index = False

    return value_column_index


def lookup_case(search_field, search_id, domain, case_type):
    """
    Attempt to find the case in CouchDB by the provided search_field and search_id.

    Returns a tuple with case (if found) and an
    error code (if there was an error in lookup).
    """
    found = False
    case_accessors = CaseAccessors(domain)
    if search_field == 'case_id':
        try:
            case = case_accessors.get_case(search_id)
            if case.domain == domain and case.type == case_type:
                found = True
        except CaseNotFound:
            pass
    elif search_field == EXTERNAL_ID:
        cases_by_type = case_accessors.get_cases_by_external_id(search_id, case_type=case_type)
        if not cases_by_type:
            return (None, LookupErrors.NotFound)
        elif len(cases_by_type) > 1:
            return (None, LookupErrors.MultipleResults)
        else:
            case = cases_by_type[0]
            found = True

    if found:
        return (case, None)
    else:
        return (None, LookupErrors.NotFound)


def populate_updated_fields(config, columns, row):
    """
    Returns a dict map of fields that were marked to be updated
    due to the import. This can be then used to pass to the CaseBlock
    to trigger updates.
    """
    field_map = convert_custom_fields_to_struct(config)
    key_column_index = get_key_column_index(config, columns)
    value_column_index = get_value_column_index(config, columns)
    fields_to_update = {}
    for key in field_map:
        try:
            if key_column_index and key == row[key_column_index]:
                update_value = row[value_column_index]
            else:
                update_value = row[columns.index(key)]
        except Exception:
            continue

        if 'field_name' in field_map[key]:
            update_field_name = field_map[key]['field_name'].strip()
        else:
            # nothing was selected so don't add this value
            continue

        if update_field_name in RESERVED_FIELDS:
            raise InvalidCustomFieldNameException(_('Field name "{}" is reserved').format(update_field_name))

        if isinstance(update_value, basestring) and update_value.strip() == SCALAR_NEVER_WAS:
            # If we find any instances of blanks ('---'), convert them to an
            # actual blank value without performing any data type validation.
            # This is to be consistent with how the case export works.
            update_value = ''
        elif update_value is not None:
            update_value = convert_field_value(update_value)

        fields_to_update[update_field_name] = update_value

    return fields_to_update


def get_spreadsheet(download_ref, column_headers=True):
    if not download_ref:
        raise ImporterRefError('null download ref')
    return ExcelFile(download_ref.get_filename(), column_headers)


def is_valid_location_owner(owner, domain):
    if isinstance(owner, Location):
        return owner.sql_location.domain == domain and owner.sql_location.location_type.shares_cases
    else:
        return False


def is_valid_id(uploaded_id, domain, cache):
    if uploaded_id in cache:
        return cache[uploaded_id]

    owner = get_wrapped_owner(uploaded_id)
    return is_valid_owner(owner, domain)


def is_valid_owner(owner, domain):
    return (
        (isinstance(owner, CouchUser) and owner.is_member_of(domain)) or
        (isinstance(owner, Group) and owner.case_sharing and owner.is_member_of(domain)) or
        is_valid_location_owner(owner, domain)
    )


def get_id_from_name(name, domain, cache):
    '''
    :param name: A username, group name, or location name/site_code
    :param domain:
    :param cache:
    :return: Looks for the given name and returns the corresponding id if the
    user or group exists and None otherwise. Searches for user first, then
    group, then location
    '''
    if name in cache:
        return cache[name]

    def get_from_user(name):
        try:
            name_as_address = name
            if '@' not in name_as_address:
                name_as_address = format_username(name, domain)
            user = CouchUser.get_by_username(name_as_address)
            return getattr(user, 'couch_id', None)
        except NoResultFound:
            return None

    def get_from_group(name):
        group = Group.by_name(domain, name, one=True)
        return getattr(group, 'get_id', None)

    def get_from_location(name):
        try:
            return SQLLocation.objects.get_from_user_input(domain, name).location_id
        except SQLLocation.DoesNotExist:
            return None

    id = get_from_user(name) or get_from_group(name) or get_from_location(name)
    cache[name] = id
    return id


def get_case_properties_for_case_type(domain, case_type):
    if should_use_sql_backend(domain):
        from corehq.apps.export.models import CaseExportDataSchema
        from corehq.apps.export.models.new import MAIN_TABLE
        schema = CaseExportDataSchema.generate_schema_from_builds(
            domain,
            None,
            case_type,
        )
        group_schemas = [gs for gs in schema.group_schemas if gs.path == MAIN_TABLE]
        if group_schemas:
            return sorted(set([item.path[0].name for item in group_schemas[0].items]))
    else:
        from corehq.apps.hqcase.dbaccessors import get_case_properties
        return get_case_properties(domain, case_type)


def get_importer_error_message(e):
    if isinstance(e, ImporterRefError):
        # I'm not totally sure this is the right error, but it's what was being
        # used before. (I think people were just calling _spreadsheet_expired
        # or otherwise blaming expired sessions whenever anything unexpected
        # happened though...)
        return _('Sorry, your session has expired. Please start over and try again.')
    elif isinstance(e, ImporterFileNotFound):
        return _('The session containing the file you uploaded has expired '
                 '- please upload a new one.')
    elif isinstance(e, ImporterExcelFileEncrypted):
        return _('The file you want to import is password protected. '
                 'Please choose a file that is not password protected.')
    elif isinstance(e, ImporterExcelError):
        return _("The file uploaded has the following error: {}").format(e.message)
    elif isinstance(e, ImporterError):
        return _("The session containing the file you uploaded has expired - please upload a new one.")
    else:
        return _("Error: {}").format(e.message)