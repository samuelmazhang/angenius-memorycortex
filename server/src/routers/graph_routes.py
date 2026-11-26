"""Route declarations for the ``/graph`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/graph/communities', H['graph_detect_communities'], methods=['POST'])
    router.add_api_route('/graph/communities', H['graph_list_communities'], methods=['GET'])
    router.add_api_route('/graph/khop', H['graph_khop'], methods=['GET'])
    router.add_api_route('/graph/sample', H['graph_sample'], methods=['GET'])
    router.add_api_route('/graph/mine', H['graph_mine'], methods=['POST'])
