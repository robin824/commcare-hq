from corehq.pillows.base import DEFAULT_META

from corehq.pillows.mappings.case_mapping import CASE_ES_TYPE
from corehq.pillows.mappings.utils import mapping_from_json
from corehq.util.elastic import es_index
from pillowtop.es_utils import ElasticsearchIndexInfo


CASE_SEARCH_INDEX = es_index("case_search_2016-03-15")
CASE_SEARCH_ALIAS = "case_search"
CASE_SEARCH_MAX_RESULTS = 10
CASE_SEARCH_MAPPING = mapping_from_json('case_search_mapping.json')


CASE_SEARCH_INDEX_INFO = ElasticsearchIndexInfo(
    index=CASE_SEARCH_INDEX,
    alias=CASE_SEARCH_ALIAS,
    type=CASE_ES_TYPE,
    meta=DEFAULT_META,
    mapping=CASE_SEARCH_MAPPING,
)
