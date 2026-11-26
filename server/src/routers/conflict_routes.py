"""Route declarations for the ``/conflict`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/conflict/upsert', H['conflict_upsert'], methods=['POST'])
    router.add_api_route('/conflict/scan', H['conflict_scan'], methods=['POST'])
    router.add_api_route('/conflict/log', H['conflict_log'], methods=['GET'])
