"""Route declarations for the ``/access-stats`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/access-stats', H['access_stats'], methods=['GET'])
    router.add_api_route('/access-stats/{memory_id}', H['access_stats_for'], methods=['GET'])
