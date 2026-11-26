"""Route declarations for the ``/admin`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/admin/deprecate-by-type', H['admin_deprecate_by_type'], methods=['POST'])
    router.add_api_route('/admin/maintenance/stats', H['maintenance_stats'], methods=['GET'])
    router.add_api_route('/admin/maintenance/dead', H['maintenance_dead'], methods=['GET'])
    router.add_api_route('/admin/maintenance/run-once', H['maintenance_run_once'], methods=['POST'])
    router.add_api_route('/admin/maintenance/enqueue/{chunk_id}', H['maintenance_enqueue'], methods=['POST'])
    router.add_api_route('/admin/maintenance/flush', H['maintenance_flush'], methods=['POST'])
