"""Route declarations for the ``/taxonomy`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/taxonomy/relations', H['taxonomy_relations'], methods=['GET'])
    router.add_api_route('/taxonomy/ancestors/{child_tag}', H['taxonomy_ancestors'], methods=['GET'])
    router.add_api_route('/taxonomy/relations/{child_tag}', H['taxonomy_delete'], methods=['DELETE'])
