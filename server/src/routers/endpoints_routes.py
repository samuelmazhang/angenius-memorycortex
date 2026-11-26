"""Route declarations for the ``/endpoints`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/endpoints', H['list_endpoints'], methods=['GET'])
    router.add_api_route('/endpoints/recommend/{scenario}', H['recommend'], methods=['GET'])
