"""Route declarations for the ``/working`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/working/set', H['working_set'], methods=['POST'])
    router.add_api_route('/working/{session_id}/context', H['working_context'], methods=['GET'])
    router.add_api_route('/working/{session_id}/append', H['working_append'], methods=['POST'])
    router.add_api_route('/working/{session_id}', H['working_get'], methods=['GET'])
    router.add_api_route('/working/{session_id}/{key}', H['working_get_key'], methods=['GET'])
