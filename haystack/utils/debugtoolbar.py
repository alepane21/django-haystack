import django
from django.utils.translation import ugettext_lazy as _
from django.utils.html import escape
from django.utils.safestring import mark_safe
from debug_toolbar.panels import DebugPanel
from debug_toolbar.utils import tidy_stacktrace

import haystack.backends

class HaystackDebugPanel(DebugPanel):
    """
    Panel that displays the Haystack queries.
    """
    name = 'Haystack'
    template = 'debug_toolbar/panels/sql.html'
    has_content = True
    
    def __init__(self, *args, **kwargs):
        super(HaystackDebugPanel, self).__init__(*args, **kwargs)
        self._queries = haystack.backends.queries
    
    def nav_title(self):
        return _('Haystack queries')

    def nav_subtitle(self):
        return "%s queries" % len(self._queries)

    def url(self):
        return ''

    def title(self):
        return 'Haystack Queries'
    
    def _transform_row(self, row):
        data = {
            'sql': unicode(row['query_string']) + unicode(row['additional_args']) + unicode(row['additional_kwargs']),
            'duration': row['time'],
            'width_ratio_relative': 0,
            'start_offset': 0
        }
        stacktrace = []
        if 'stacktrace' in row:
            row['stacktrace'] = reversed(tidy_stacktrace(row['stacktrace']))
            for frame in row['stacktrace']:
                params = map(escape, frame[0].rsplit('/', 1) + list(frame[1:]))
                try:
                    stacktrace.append(u'<span class="path">{0}/</span><span class="file">{1}</span> in <span class="func">{3}</span>(<span class="lineno">{2}</span>)\n  <span class="code">{4}</span>'.format(*params))
                except IndexError:
                    # This frame doesn't have the expected format, so skip it and move on to the next one
                    continue
            data['stacktrace'] = mark_safe('\n'.join(stacktrace))
        return data

    def process_response(self, request, response):
        self.record_stats({
            'databases': [],
            'queries': [self._transform_row(q) for q in self._queries],
            'duration': 0,
        })
