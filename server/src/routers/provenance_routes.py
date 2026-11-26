"""Route declarations for the ``/provenance`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/provenance/audit', H['provenance_audit'], methods=['GET'])
    router.add_api_route('/provenance/{memory_id}', H['get_provenance'], methods=['GET'])
