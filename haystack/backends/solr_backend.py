import logging
import sys
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models.loading import get_model
from haystack.backends import BaseSearchBackend, BaseSearchQuery, log_query, EmptyResults
from haystack.constants import ID, DJANGO_CT, DJANGO_ID
from haystack.exceptions import MissingDependency, MoreLikeThisError
from haystack.models import SearchResult
from haystack.utils import get_identifier
try:
    from django.db.models.sql.query import get_proxied_model
except ImportError:
    # Likely on Django 1.0
    get_proxied_model = None
try:
    from pysolr import Solr, SolrError
except ImportError:
    raise MissingDependency("The 'solr' backend requires the installation of 'pysolr'. Please refer to the documentation.")


BACKEND_NAME = 'solr'


class SearchBackend(BaseSearchBackend):
    # Word reserved by Solr for special use.
    RESERVED_WORDS = (
        'AND',
        'NOT',
        'OR',
        'TO',
    )
    
    # Characters reserved by Solr for special use.
    # The '\\' must come first, so as not to overwrite the other slash replacements.
    RESERVED_CHARACTERS = (
        '\\', '+', '-', '&&', '||', '!', '(', ')', '{', '}',
        '[', ']', '^', '"', '~', '*', '?', ':',
    )
    
    DISMAX_PARAMETERS = (
        'defType',
        'qf',
        'q.alt',
        'mm',
        'pf',
        'ps',
        'qs',
        'tie',
        'bq',
        'bf',
        'boost',
    )
    
    def __init__(self, site=None):
        super(SearchBackend, self).__init__(site)
        
        if not hasattr(settings, 'HAYSTACK_SOLR_URLS'):
            raise ImproperlyConfigured('You must specify a HAYSTACK_SOLR_URLS in your settings.')
        
        timeout = getattr(settings, 'HAYSTACK_SOLR_TIMEOUT', 10)
        self.conn = Solr(settings.HAYSTACK_SOLR_URLS['MASTER'], timeout=timeout)
        self.conn_slave = Solr(settings.HAYSTACK_SOLR_URLS['SLAVE'], timeout=timeout)
        self.log = logging.getLogger('haystack')
    
    def update(self, index, iterable, commit=True):
        docs = []
        
        try:
            for obj in iterable:
                docs.append(index.full_prepare(obj))
        except UnicodeDecodeError:
            sys.stderr.write("Chunk failed.\n")
        
        if len(docs) > 0:
            try:
                self.conn.add(docs, commit=commit, boost=index.get_field_weights())
            except (IOError, SolrError), e:
                self.log.error("Failed to add documents to Solr: %s", e)
    
    def remove(self, obj_or_string, commit=True):
        solr_id = get_identifier(obj_or_string)
        
        try:
            kwargs = {
                'commit': commit,
                ID: solr_id
            }
            self.conn.delete(**kwargs)
        except (IOError, SolrError), e:
            self.log.error("Failed to remove document '%s' from Solr: %s", solr_id, e)
    
    def clear(self, models=[], commit=True):
        try:
            if not models:
                # *:* matches all docs in Solr
                self.conn.delete(q='*:*', commit=commit)
            else:
                models_to_delete = []
                
                for model in models:
                    models_to_delete.append("%s:%s.%s" % (DJANGO_CT, model._meta.app_label, model._meta.module_name))
                
                self.conn.delete(q=" OR ".join(models_to_delete), commit=commit)
            
            # Run an optimize post-clear. http://wiki.apache.org/solr/FAQ#head-9aafb5d8dff5308e8ea4fcf4b71f19f029c4bb99
            self.conn.optimize()
        except (IOError, SolrError), e:
            if len(models):
                self.log.error("Failed to clear Solr index of models '%s': %s", ','.join(models_to_delete), e)
            else:
                self.log.error("Failed to clear Solr index: %s", e)
    
    @log_query
    def search(self, query_string, sort_by=None, start_offset=0, end_offset=None,
               fields='', highlight=False, facets=None, date_facets=None, query_facets=None,
               pivot_facets=None, narrow_queries=None, spelling_query=None, facet_mincount=None, facet_limit=None,
               facet_field_limit=None, facet_prefix=None, facet_sort=None, facet_pivot_mincount=None, dismax=None,
               limit_to_registered_models=None, result_class=None, **kwargs):
        
        if len(query_string) == 0:
            return {
                'results': [],
                'hits': 0,
            }
        
        kwargs = {
            'fl': '* score',
        }
        
        if fields:
            kwargs['fl'] = fields
        
        if sort_by is not None:
            kwargs['sort'] = sort_by
        
        if start_offset is not None:
            kwargs['start'] = start_offset
        
        if end_offset is not None:
            kwargs['rows'] = end_offset - start_offset
        
        if highlight is True:
            kwargs['hl'] = 'true'
            kwargs['hl.fragsize'] = '200'
        
        if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
            kwargs['spellcheck'] = 'true'
            kwargs['spellcheck.collate'] = 'true'
            kwargs['spellcheck.count'] = 1
            
            if spelling_query:
                kwargs['spellcheck.q'] = spelling_query
        
        if facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.field'] = facets
            
        if facet_mincount is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.mincount'] = facet_mincount
            
        if facet_limit is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.limit'] = facet_limit
            
        if facet_field_limit is not None:
            kwargs['facet'] = 'on'
            for f, limit in facet_field_limit.iteritems():
                kwargs['f.%s.facet.limit' % f] = limit
        
        if facet_prefix is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.prefix'] = facet_prefix

        if facet_sort is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.sort'] = facet_sort
        
        if date_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.date'] = date_facets.keys()
            kwargs['facet.date.other'] = 'none'
            
            for key, value in date_facets.items():
                kwargs["f.%s.facet.date.start" % key] = self.conn_slave._from_python(value.get('start_date'))
                kwargs["f.%s.facet.date.end" % key] = self.conn_slave._from_python(value.get('end_date'))
                gap_by_string = value.get('gap_by').upper()
                gap_string = "%d%s" % (value.get('gap_amount'), gap_by_string)
                
                if value.get('gap_amount') != 1:
                    gap_string += "S"
                
                kwargs["f.%s.facet.date.gap" % key] = '+%s/%s' % (gap_string, gap_by_string)
        
        if query_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.query'] = ["%s:%s" % (field, value) for field, value in query_facets]
            
        if pivot_facets is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.pivot'] = pivot_facets
            
        if facet_pivot_mincount is not None:
            kwargs['facet'] = 'on'
            kwargs['facet.pivot_mincount'] = facet_pivot_mincount
        
        if limit_to_registered_models is None:
            limit_to_registered_models = getattr(settings, 'HAYSTACK_LIMIT_TO_REGISTERED_MODELS', True)
        
        if limit_to_registered_models:
            # Using narrow queries, limit the results to only models registered
            # with the current site.
            if narrow_queries is None:
                narrow_queries = set()
            
            registered_models = self.build_registered_models_list()
            
            if len(registered_models) > 0:
                narrow_queries.add('%s:(%s)' % (DJANGO_CT, ' OR '.join(registered_models)))
        
        if narrow_queries is not None:
            kwargs['fq'] = list(narrow_queries)
            
        if dismax is not None:
            kwargs.update(dismax)
        
        try:
            raw_results = self.conn_slave.search(query_string, **kwargs)
        except (IOError, SolrError), e:
            self.log.error("Failed to query Solr using '%s': %s", query_string, e)
            raw_results = EmptyResults()
        
        return self._process_results(raw_results, highlight=highlight, result_class=result_class)
    
    def more_like_this(self, model_instance, additional_query_string=None,
                       start_offset=0, end_offset=None,
                       limit_to_registered_models=None, result_class=None, **kwargs):
        # Handle deferred models.
        if get_proxied_model and hasattr(model_instance, '_deferred') and model_instance._deferred:
            model_klass = get_proxied_model(model_instance._meta)
        else:
            model_klass = type(model_instance)
        
        index = self.site.get_index(model_klass)
        field_name = index.get_content_field()
        params = {
            'fl': '*,score',
        }
        
        if start_offset is not None:
            params['start'] = start_offset
        
        if end_offset is not None:
            params['rows'] = end_offset
        
        narrow_queries = set()
        
        if limit_to_registered_models is None:
            limit_to_registered_models = getattr(settings, 'HAYSTACK_LIMIT_TO_REGISTERED_MODELS', True)
        
        if limit_to_registered_models:
            # Using narrow queries, limit the results to only models registered
            # with the current site.
            if narrow_queries is None:
                narrow_queries = set()
            
            registered_models = self.build_registered_models_list()
            
            if len(registered_models) > 0:
                narrow_queries.add('%s:(%s)' % (DJANGO_CT, ' OR '.join(registered_models)))
        
        if additional_query_string:
            narrow_queries.add(additional_query_string)
        
        if narrow_queries:
            params['fq'] = list(narrow_queries)
        
        query = "%s:%s" % (ID, get_identifier(model_instance))
        
        try:
            raw_results = self.conn_slave.more_like_this(query, field_name, **params)
        except (IOError, SolrError), e:
            self.log.error("Failed to fetch More Like This from Solr for document '%s': %s", query, e)
            raw_results = EmptyResults()
        
        return self._process_results(raw_results, result_class=result_class)
    
    def _process_results(self, raw_results, highlight=False, result_class=None):
        if not self.site:
            from haystack import site
        else:
            site = self.site
        
        results = []
        hits = raw_results.hits
        facets = {}
        spelling_suggestion = None
        
        if result_class is None:
            result_class = SearchResult
            
        if hasattr(raw_results, 'facets'):
            facets = {
                'fields': raw_results.facets.get('facet_fields', {}),
                'dates': raw_results.facets.get('facet_dates', {}),
                'queries': raw_results.facets.get('facet_queries', {}),
                'pivots': raw_results.facets.get('facet_pivot', {}),
            }
            
            for key in ['fields']:
                for facet_field in facets[key]:
                    # Convert to a two-tuple, as Solr's json format returns a list of
                    # pairs.
                    facets[key][facet_field] = zip(facets[key][facet_field][::2], facets[key][facet_field][1::2])
                    
            def _process_pivot(pivot):
                facet = []
                for p in pivot:
                    if not 'pivot' in p:
                        facet.append((str(p['value']), p['count'], ()))
                    else:
                        facet.append((str(p['value']), p['count'], tuple(_process_pivot(p['pivot']))))
                
                return facet
                    
            for key in ['pivots']:
                for facet_pivot in facets[key]:
                    # Convert to a three-tuple, with pairs + nested pivot
                    facets[key][facet_pivot] = _process_pivot(facets[key][facet_pivot])
                    
        if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
            if hasattr(raw_results, 'spellcheck'):
                if len(raw_results.spellcheck.get('suggestions', [])):
                    # For some reason, it's an array of pairs. Pull off the
                    # collated result from the end.
                    spelling_suggestion = raw_results.spellcheck.get('suggestions')[-1]
        
        indexed_models = site.get_indexed_models()
        
        for raw_result in raw_results.docs:
            app_label, model_name = raw_result[DJANGO_CT].split('.')
            additional_fields = {}
            model = get_model(app_label, model_name)
            
            if model and model in indexed_models:
                for key, value in raw_result.items():
                    index = site.get_index(model)
                    string_key = str(key)
                    
                    if string_key in index.fields and hasattr(index.fields[string_key], 'convert'):
                        additional_fields[string_key] = index.fields[string_key].convert(value)
                    else:
                        additional_fields[string_key] = self.conn_slave._to_python(value)
                
                for name in [DJANGO_CT, DJANGO_ID, 'score']:
                    if name in additional_fields:
                        del(additional_fields[name])
                
                if raw_result[ID] in getattr(raw_results, 'highlighting', {}):
                    additional_fields['highlighted'] = raw_results.highlighting[raw_result[ID]]
                
                result = result_class(app_label, model_name, raw_result.get(DJANGO_ID), raw_result.get('score'), searchsite=self.site, **additional_fields)
                results.append(result)
            else:
                hits -= 1
        
        return {
            'results': results,
            'hits': hits,
            'facets': facets,
            'spelling_suggestion': spelling_suggestion,
        }
    
    def build_schema(self, fields):
        content_field_name = ''
        schema_fields = []
        
        for field_name, field_class in fields.items():
            field_data = {
                'field_name': field_class.index_fieldname,
                'type': 'text',
                'indexed': 'true',
                'stored': 'true',
                'multi_valued': 'false',
            }
            
            if field_class.document is True:
                content_field_name = field_class.index_fieldname
            
            # DRL_FIXME: Perhaps move to something where, if none of these
            #            checks succeed, call a custom method on the form that
            #            returns, per-backend, the right type of storage?
            if field_class.field_type in ['date', 'datetime']:
                field_data['type'] = 'date'
            elif field_class.field_type == 'integer':
                field_data['type'] = 'slong'
            elif field_class.field_type == 'float':
                field_data['type'] = 'sfloat'
            elif field_class.field_type == 'boolean':
                field_data['type'] = 'boolean'
            elif field_class.field_type == 'ngram':
                field_data['type'] = 'ngram'
            elif field_class.field_type == 'edge_ngram':
                field_data['type'] = 'edge_ngram'
            
            if field_class.is_multivalued:
                field_data['multi_valued'] = 'true'
            
            if field_class.stored is False:
                field_data['stored'] = 'false'
            
            # Do this last to override `text` fields.
            if field_class.indexed is False:
                field_data['indexed'] = 'false'
                
                # If it's text and not being indexed, we probably don't want
                # to do the normal lowercase/tokenize/stemming/etc. dance.
                if field_data['type'] == 'text':
                    field_data['type'] = 'string'
            
            # If it's a ``FacetField``, make sure we don't postprocess it.
            if hasattr(field_class, 'facet_for'):
                # If it's text, it ought to be a string.
                if field_data['type'] == 'text':
                    field_data['type'] = 'string'
            
            schema_fields.append(field_data)
        
        return (content_field_name, schema_fields)


class SearchQuery(BaseSearchQuery):
    def __init__(self, site=None, backend=None):
        super(SearchQuery, self).__init__(site, backend)
        
        if backend is not None:
            self.backend = backend
        else:
            self.backend = SearchBackend(site=site)

    def matching_all_fragment(self):
        return '*:*'

    def build_query_fragment(self, field, filter_type, value):
        result = ''
        
        # Handle when we've got a ``ValuesListQuerySet``...
        if hasattr(value, 'values_list'):
            value = list(value)
        
        if not isinstance(value, (list, tuple)):
            # Convert whatever we find to what pysolr wants.
            value = self.backend.conn_slave._from_python(value)
        
        # Check to see if it's a phrase for an exact match.
        if ' ' in value:
            value = '"%s"' % value
        
        index_fieldname = self.backend.site.get_index_fieldname(field)
        
        # 'content' is a special reserved word, much like 'pk' in
        # Django's ORM layer. It indicates 'no special field'.
        if field == 'content':
            result = value
        else:
            filter_types = {
                'exact': "%s:%s",
                'gt': "%s:{%s TO *}",
                'gte': "%s:[%s TO *]",
                'lt': "%s:{* TO %s}",
                'lte': "%s:[* TO %s]",
                'startswith': "%s:%s*",
            }
            
            if filter_type == 'in':
                in_options = []
                
                for possible_value in value:
                    in_options.append('%s:"%s"' % (index_fieldname, self.backend.conn_slave._from_python(possible_value)))
                
                result = "(%s)" % " OR ".join(in_options)
            elif filter_type == 'range':
                start = self.backend.conn._from_python(value[0])
                end = self.backend.conn._from_python(value[1])
                return "%s:[%s TO %s]" % (index_fieldname, start, end)
            else:
                result = filter_types[filter_type] % (index_fieldname, value)
        
        return result
    
    def build_facet_field(self, facet, key, ex):
        f_mask = ''
        tok = []
        if key or ex:
            f_mask += '{!'
        if key:
            f_mask += 'key=%s'
            tok.append(key)
        if ex:
            if key:
                f_mask += ' ' 
            f_mask += 'ex=%s'
            tok.append(ex)
        if key or ex:
            f_mask += '}'
        f_mask += facet
        if len(tok) > 0:
            facet = f_mask % tuple(tok)

        return facet
    
    def run(self, spelling_query=None):
        """Builds and executes the query. Returns a list of search results."""
        final_query = self.build_query()
        kwargs = {
            'start_offset': self.start_offset,
            'result_class': self.result_class,
        }
        
        if self.order_by:
            order_by_list = []
            
            for order_by in self.order_by:
                if order_by.startswith('-'):
                    order_by_list.append('%s desc' % order_by[1:])
                else:
                    order_by_list.append('%s asc' % order_by)
            
            kwargs['sort_by'] = ", ".join(order_by_list)
        
        if self.end_offset is not None:
            kwargs['end_offset'] = self.end_offset
        
        if self.highlight:
            kwargs['highlight'] = self.highlight

        if self.facets:
            facets = []
            for facet, key, ex in list(self.facets):
                if facet:
                    facets.append(self.build_facet_field(facet, key, ex))
            kwargs['facets'] = facets
        
        if self.date_facets:
            kwargs['date_facets'] = self.date_facets
            
        if self.facet_mincount:
            kwargs['facet_mincount'] = self.facet_mincount
        
        if self.facet_limit:
            kwargs['facet_limit'] = self.facet_limit
        
        if self.facet_field_limit:
            kwargs['facet_field_limit'] = self.facet_field_limit
        
        if self.facet_prefix:
            kwargs['facet_prefix'] = self.facet_prefix

        if self.facet_sort:
            kwargs['facet_sort'] = self.facet_sort
            
        if self.facet_pivot_mincount:
            kwargs['facet_mincount'] = self.facet_mincount
        
        if self.query_facets:
            kwargs['query_facets'] = self.query_facets
            
        if self.pivot_facets:
            facets = []
            for facet, key, ex in list(self.pivot_facets):
                if facet:
                    facets.append(self.build_facet_field(facet, key, ex))
            kwargs['pivot_facets'] = facets
        
        if self.narrow_queries:
            narrow_queries = []
            for query, tag in list(self.narrow_queries):
                if query and tag:
                    query = '{!tag=%s}' % tag + query
                narrow_queries.append(query)

            kwargs['narrow_queries'] = narrow_queries
        
        if self.dismax:
            kwargs['dismax'] = self.dismax
        
        if spelling_query:
            kwargs['spelling_query'] = spelling_query
        
        results = self.backend.search(final_query, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)
        self._facet_counts = self.post_process_facets(results)
        self._spelling_suggestion = results.get('spelling_suggestion', None)
    
    def run_mlt(self):
        """Builds and executes the query. Returns a list of search results."""
        if self._more_like_this is False or self._mlt_instance is None:
            raise MoreLikeThisError("No instance was provided to determine 'More Like This' results.")
        
        additional_query_string = self.build_query()
        kwargs = {
            'start_offset': self.start_offset,
            'result_class': self.result_class,
        }
        
        if self.end_offset is not None:
            kwargs['end_offset'] = self.end_offset - self.start_offset
        
        results = self.backend.more_like_this(self._mlt_instance, additional_query_string, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)

    def add_field_facet(self, field, key=None, ex=[]):
        """Adds a regular facet on a field."""
        facet_field = self.backend.site.get_facet_field_name(field)
        self.facets.add((facet_field, key, ','.join(ex)))
        
    def add_narrow_query(self, query, tag=[]):
        """
        Narrows a search to a subset of all documents per the query.
        
        Generally used in conjunction with faceting.
        """
        self.narrow_queries.add((query, ','.join(tag)))
        
    def add_pivot_facet(self, *args, **kwargs):
        """Adds pivot faceting to a query for the provided fields."""
        key = kwargs.get('key', None)
        ex = kwargs.get('ex', [])
        facet_fields = []
        for field in args:
            facet_fields.append(self.backend.site.get_facet_field_name(field))
        self.pivot_facets.add((','.join(facet_fields), key, ','.join(ex)))
        
    def post_process_facets(self, results):
        # Handle renaming the facet fields. Undecorate and all that.
        revised_facets = {}
        field_data = self.backend.site.all_searchfields()
        
        for facet_type, field_details in results.get('facets', {}).items():
            temp_facets = {}
            
            for field, field_facets in field_details.items():
                fieldname = []
                
                if facet_type in ['pivots']:
                    field = field.split(',')
                else:
                    field = [field]
                
                for f in field:
                    if f in field_data and hasattr(field_data[f], 'get_facet_for_name'):
                        fieldname.append(field_data[f].get_facet_for_name())
                
                if len(fieldname) == 0:
                    fieldname = field[0]
                else:
                    fieldname = ','.join(fieldname)
                    
                temp_facets[fieldname] = field_facets
            
            revised_facets[facet_type] = temp_facets
        
        return revised_facets
