"""Route declarations for the ``/hierarchy`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/hierarchy', H['create_hierarchy'], methods=['POST'])
    router.add_api_route('/hierarchy/{node_id}', H['get_hierarchy_node'], methods=['GET'])
    router.add_api_route('/hierarchy/enrich/{chunk_id}', H['enrich_chunk_with_hierarchy'], methods=['GET'])
    router.add_api_route('/hierarchy/{node_id}/tree', H['get_hierarchy_tree'], methods=['GET'])
    router.add_api_route('/hierarchy/enrich/{chunk_id}', H['hierarchy_enrich'], methods=['GET'])
