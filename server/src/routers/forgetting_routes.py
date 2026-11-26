"""Route declarations for the ``/forgetting`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/forgetting/stats', H['forgetting_stats'], methods=['GET'])
    router.add_api_route('/forgetting/expired', H['forgetting_expired'], methods=['GET'])
    router.add_api_route('/forgetting/expiring-soon', H['forgetting_expiring'], methods=['GET'])
    router.add_api_route('/forgetting/cleanup', H['forgetting_cleanup'], methods=['POST'])
    router.add_api_route('/forgetting/guided', H['guided_forgetting'], methods=['POST'])
