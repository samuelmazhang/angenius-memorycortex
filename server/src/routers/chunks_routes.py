"""Route declarations for the ``/chunks`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/chunks', H['create_chunk'], methods=['POST'])
    router.add_api_route('/chunks/batch', H['create_chunks_batch'], methods=['POST'])
    router.add_api_route('/chunks/{chunk_id}', H['get_chunk'], methods=['GET'])
    router.add_api_route('/chunks/{chunk_id}', H['update_chunk'], methods=['POST'])
    router.add_api_route('/chunks/{chunk_id}', H['patch_chunk'], methods=['PATCH'])
    router.add_api_route('/chunks/{chunk_id}', H['delete_chunk'], methods=['DELETE'])
