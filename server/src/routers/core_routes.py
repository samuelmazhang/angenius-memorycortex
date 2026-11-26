"""Route declarations for the ``/core`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/core/{user_id}', H['get_core'], methods=['GET'])
    router.add_api_route('/core', H['get_core_list'], methods=['GET'])
    router.add_api_route('/core/{user_id}', H['upsert_core'], methods=['POST'])
