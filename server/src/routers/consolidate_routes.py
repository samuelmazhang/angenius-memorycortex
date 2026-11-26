"""Route declarations for the ``/consolidate`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/consolidate/extract', H['consolidate_extract'], methods=['POST'])
    router.add_api_route('/consolidate/run', H['consolidate_run'], methods=['POST'])
    router.add_api_route('/consolidate/facts', H['consolidate_facts'], methods=['GET'])
