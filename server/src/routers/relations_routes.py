"""Route declarations for the ``/relations`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/relations/batch', H['get_relations_batch'], methods=['GET'])
    router.add_api_route('/relations/{memory_id}', H['get_relations'], methods=['GET'])
    router.add_api_route('/relations/{memory_id}', H['delete_relations'], methods=['DELETE'])
    router.add_api_route('/relations/{memory_id}/{target_id}', H['delete_specific_relation'], methods=['DELETE'])
    router.add_api_route('/relations/temporal', H['create_temporal_relation'], methods=['POST'])
    router.add_api_route('/relations/prune', H['relations_prune'], methods=['POST'])
    router.add_api_route('/relations/temporal/{source_id}', H['get_temporal_relations'], methods=['GET'])
