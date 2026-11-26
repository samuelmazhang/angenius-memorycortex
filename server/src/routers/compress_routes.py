"""Route declarations for the ``/compress`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/compress/run', H['compress_run'], methods=['POST'])
    router.add_api_route('/compress/{source_chunk_id}', H['get_compressed'], methods=['GET'])
