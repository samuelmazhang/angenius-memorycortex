"""Route declarations for the ``/cache`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/cache/pr/stats', H['pr_cache_stats'], methods=['GET'])
    router.add_api_route('/cache/pr/clear', H['clear_pr_cache'], methods=['POST'])
