"""Route declarations for the ``/stats`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/stats', H['stats'], methods=['GET'])
    router.add_api_route('/stats/by-type', H['stats_by_type'], methods=['GET'])
    router.add_api_route('/stats/by-source', H['stats_by_source'], methods=['GET'])
    router.add_api_route('/stats/by-status', H['stats_by_status'], methods=['GET'])
    router.add_api_route('/stats/by-deprecated', H['stats_by_deprecated'], methods=['GET'])
    router.add_api_route('/stats/by-importance', H['stats_by_importance'], methods=['GET'])
    router.add_api_route('/stats/ops', H['stats_ops'], methods=['GET'])
    router.add_api_route('/stats/embed-cache', H['stats_embed_cache'], methods=['GET'])
    router.add_api_route('/stats/health', H['stats_health'], methods=['GET'])
    router.add_api_route('/stats/ops/reset', H['reset_ops'], methods=['POST'])
