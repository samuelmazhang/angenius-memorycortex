"""Route declarations for the ``/procedural`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/procedural', H['create_procedural'], methods=['POST'])
    router.add_api_route('/procedural/search', H['search_procedural'], methods=['GET'])
    router.add_api_route('/procedural/{proc_id}/outcome', H['procedural_outcome'], methods=['POST'])
