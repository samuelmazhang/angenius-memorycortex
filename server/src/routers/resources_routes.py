"""Route declarations for the ``/resources`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/resources', H['create_resource'], methods=['POST'])
    router.add_api_route('/resources/search', H['search_resources'], methods=['GET'])
    router.add_api_route('/resources/{resource_id}/accessed', H['resource_accessed'], methods=['POST'])
