"""Route declarations for the ``/storage`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/storage/health', H['storage_health'], methods=['GET'])
    router.add_api_route('/storage/evict', H['storage_evict'], methods=['POST'])
    router.add_api_route('/storage/enforce', H['storage_enforce'], methods=['POST'])
