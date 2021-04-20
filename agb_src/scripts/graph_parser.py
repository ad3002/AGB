import re
import sys
import subprocess
from os.path import basename
from collections import defaultdict

import gfapy
import networkx as nx

from agb_src.scripts.config import *
from agb_src.scripts.edge import Edge
from agb_src.scripts.utils import get_edge_agv_id, calculate_median_cov, get_edge_num, find_file_by_pattern, \
    is_empty_file, can_reuse, is_osx, is_abyss, is_spades, is_velvet, is_soap, is_sga, get_match_edge_id, \
    edge_id_to_name, get_filename, is_acgt_seq

repeat_colors = ["red", "darkgreen", "blue", "goldenrod", "cadetblue1", "darkorchid", "aquamarine1",
                 "darkgoldenrod1", "deepskyblue1", "darkolivegreen3"]


def parse_abyss_dot(dot_fpath, min_edge_len):
    '''digraph adj {
    graph [k=50]
    edge [d=-49]
    "3+" [l=99 C=454]
    "3-" [l=99 C=454]
    '''
    dict_edges = dict()
    predecessors = defaultdict(list)
    successors = defaultdict(list)

    edge_pattern = '"?(?P<edge_id>\d+)(?P<edge_sign>[\+\-])"? (?P<info>.+)'
    link_pattern = '"?(?P<start>\d+)(?P<start_sign>[\+\-])"? -> "?(?P<end>\d+)(?P<end_sign>[\+\-])"?'
    info_pattern = 'l=(?P<edge_len>\d+) C=(?P<edge_cov>\d+)'
    with open(dot_fpath) as f:
        for line in f:
            if 'l=' in line:
                #  "3+" -> "157446-" [d=-45]
                match = re.search(edge_pattern, line)
                if not match or len(match.groups()) < 3:
                    continue
                edge_id, edge_sign, info = match.group('edge_id'), match.group('edge_sign'), match.group('info')
                edge_name = (edge_sign if edge_sign != '+' else '') + edge_id
                edge_id = get_edge_agv_id(edge_name)
                match = re.search(info_pattern, info)
                if match and len(match.groups()) == 2:
                    cov = max(1, int(match.group('edge_cov')))
                    edge_len = max(1, int(float(match.group('edge_len'))))
                    if edge_len >= min_edge_len:
                        edge = Edge(edge_id, edge_name, edge_len, cov, element_id=edge_id)
                        dict_edges[edge_id] = edge
            if '->' in line:
                #  "3+" -> "157446-" [d=-45]
                match = re.search(link_pattern, line)
                if not match or len(match.groups()) < 2:
                    continue
                start, start_sign, end, end_sign = match.group('start'), match.group('start_sign'), match.group('end'), match.group('end_sign')
                start_edge_id = get_edge_agv_id((start_sign if start_sign == '-' else '') + start)
                end_edge_id = get_edge_agv_id((end_sign if end_sign == '-' else '') + end)
                predecessors[end_edge_id].append(start_edge_id)
                successors[start_edge_id].append(end_edge_id)

    dict_edges = construct_graph(dict_edges, predecessors, successors)
    return dict_edges


def parse_flye_dot(dot_fpath, min_edge_len):
    dict_edges = dict()

    pattern = '"?(?P<start>\d+)"? -> "?(?P<end>\d+)"? \[(?P<info>.+)]'
    label_pattern = 'id (?P<edge_id>\-*.+) (?P<edge_len>[0-9\.]+)k (?P<coverage>\d+)'
    with open(dot_fpath) as f:
        for line in f:
            if 'label =' in line:
                # "7" -> "29" [label = "id 1\l53k 59x", color = "black"] ;
                line = line.replace('\\l', ' ')
                match = re.search(pattern, line)
                if not match or len(match.groups()) < 3:
                    continue
                start, end, info = match.group('start'), match.group('end'), match.group('info')
                params_dict = dict(param.split(' = ') for param in info.split(', ') if '=' in param)
                # label = params_dict.get('label')
                color = params_dict.get('color').strip().replace('"', '')
                line = line.replace(' ,', ',')
                match = re.search(label_pattern, info)
                if match and match.group('edge_id'):
                    edge_id = get_edge_agv_id(match.group('edge_id'))
                    cov = max(1, int(match.group('coverage')))
                    edge_len = max(1, int(float(match.group('edge_len')) * 1000))
                    if edge_len < min_edge_len:
                        continue
                    edge = Edge(edge_id, match.group('edge_id'), edge_len, cov, element_id=edge_id)
                    edge.color = color
                    if edge.color != "black":
                        edge.repetitive = True
                    edge.start, edge.end = int(start), int(end)
                    if 'dir = both' in line:
                        edge.two_way = True
                    dict_edges[edge_id] = edge
    dict_edges = calculate_multiplicities(dict_edges)
    return dict_edges


def parse_canu_unitigs_info(input_dirpath, dict_edges):
    tiginfo_fpath = find_file_by_pattern(input_dirpath, ".unitigs.layout.tigInfo")
    if not is_empty_file(tiginfo_fpath):
        with open(tiginfo_fpath) as f:
            for i, line in enumerate(f):
                if i == 0:
                    header = line.strip().split()
                    repeat_col = header.index("sugRept") if "sugRept" in header else None
                    cov_col = header.index("coverage") if "coverage" in header else None
                    if repeat_col is None or cov_col is None:
                        break
                    continue
                fs = line.strip().split()
                edge_id = get_edge_agv_id(get_edge_num(fs[0]))
                rc_edge_id = get_edge_agv_id(-get_edge_num(fs[0]))
                if edge_id in dict_edges:
                    coverage = int(float(fs[cov_col]))
                    dict_edges[edge_id].cov = coverage
                    dict_edges[rc_edge_id].cov = coverage
                    if fs[repeat_col] == "yes":
                        dict_edges[edge_id].repetitive = True
                        dict_edges[rc_edge_id].repetitive = True
                # else:
                #    print("Warning! Edge %s is not found!" % edge_id)
    return dict_edges


def get_edges_from_gfa(gfa_fpath, output_dirpath, min_edge_len):
    if not gfa_fpath:
        return None

    input_edges_fpath = join(dirname(gfa_fpath), get_filename(gfa_fpath) + ".fasta")
    edges_fpath = join(output_dirpath, basename(input_edges_fpath))
    if not is_empty_file(gfa_fpath) and not can_reuse(edges_fpath, files_to_check=[gfa_fpath]):
        print("Extracting edge sequences from " + gfa_fpath + "...")
        with open(edges_fpath, "w") as out:
            with open(gfa_fpath) as f:
                for line in f:
                    if line.startswith('S'):
                        fs = line.strip().split()
                        seq_name = fs[1]
                        seq = None
                        if is_acgt_seq(fs[2]):
                            seq = fs[2]
                        elif len(fs) >= 4 and is_acgt_seq(fs[3]):
                            seq = fs[3]
                        if seq and len(seq) >= min_edge_len:
                            out.write(">%s\n" % get_edge_agv_id(get_edge_num(seq_name)))
                            out.write(seq)
                            out.write("\n")
    if is_empty_file(edges_fpath) and not is_empty_file(input_edges_fpath):
        with open(edges_fpath, "w") as out:
            with open(input_edges_fpath) as f:
                for line in f:
                    if line.startswith('>'):
                        seq_name = line.strip().split()[0][1:]
                        out.write(">%s\n" % get_edge_agv_id(get_edge_num(seq_name)))
                    else:
                        out.write(line)
    return edges_fpath


def format_edges_file(input_fpath, output_dirpath):
    if is_empty_file(input_fpath):
        return None
    edges_fpath = join(output_dirpath, "edges.fasta")
    if not can_reuse(edges_fpath, files_to_check=[input_fpath]):
        with open(input_fpath) as f:
            with open(edges_fpath, "w") as out_f:
                for line in f:
                    if line.startswith('>'):
                        edge_id = get_edge_agv_id(get_edge_num(line[1:]))
                        out_f.write(">%s\n" % edge_id)
                    else:
                        out_f.write(line)
    return edges_fpath


def fastg_to_gfa(input_fpath, output_dirpath, assembler_name):
    k8_exec = join(TOOLS_DIR, "k8-darwin") if is_osx() else join(TOOLS_DIR, "k8-linux")
    gfatools_exec = join(TOOLS_DIR, "gfatools.js")
    if gfatools_exec and k8_exec:
        output_fpath = join(output_dirpath, basename(input_fpath).replace("fastg", "gfa"))
        cmd = None
        if is_abyss(assembler_name):
            cmd = "abyss2gfa"
        elif is_spades(assembler_name):
            cmd = "spades2gfa"
        elif is_sga(assembler_name):
            cmd = "sga2gfa"
        elif is_soap(assembler_name):
            cmd = "soap2gfa"
        elif is_velvet(assembler_name):
            cmd = "velvet2gfa"
        if not cmd:
            sys.exit("FASTG files produced by " + assembler_name + " are not supported. Supported assemblers: " +
                     ' '.join([ABYSS_NAME, SGA_NAME, SOAP_NAME, SPADES_NAME, VELVET_NAME]) + " or use files in GFA format.")
        cmdline = [k8_exec, gfatools_exec, cmd, input_fpath]
        subprocess.call(cmdline, stdout=output_fpath, stderr=open("/dev/null", "w"))
        if not is_empty_file(output_fpath):
            return output_fpath


def parse_gfa(gfa_fpath, min_edge_len, input_dirpath=None, assembler=None):
    dict_edges = dict()
    predecessors = defaultdict(list)
    successors = defaultdict(list)
    g = nx.DiGraph()

    print("Parsing " + gfa_fpath + "...")
    # gfa = gfapy.Gfa.from_file(gfa_fpath, vlevel = 0)
    links = []
    edge_overlaps = defaultdict(dict)
    with open(gfa_fpath) as f:
        for line in f:
            record_type = line[0]
            if record_type == 'S':
                fs = line.split()
                name, seq_len = fs[1], len(fs[2])
                if fs[2] == '*':
                    seq_len = None
                add_fields = fs[3:] if len(fs) > 3 else []
                add_info = dict((f.split(':')[0].lower(), f.split(':')[-1]) for f in add_fields)
                cov = 1
                if "dp" in add_info:
                    cov = float(add_info["dp"])  ## coverage depth
                elif "kc" in add_info:
                    cov = max(1, int(add_info["kc"]) / seq_len)  ## k-mer count / edge length
                if "ln" in add_info:
                    seq_len = int(add_info["ln"])  ## sequence length
                if seq_len and seq_len >= min_edge_len:
                    edge_id = get_edge_agv_id(get_edge_num(name))
                    edge = Edge(edge_id, get_edge_num(name), seq_len, cov, element_id=edge_id)
                    dict_edges[edge_id] = edge
                    for overlapped_edge, overlap in edge_overlaps[edge_id].items():
                        dict_edges[edge_id].overlaps.append((edge_id_to_name(overlapped_edge), overlapped_edge, overlap))
                    rc_edge_id = get_edge_agv_id(-get_edge_num(name))
                    rc_edge = Edge(rc_edge_id, -get_edge_num(name), seq_len, cov, element_id=rc_edge_id)
                    dict_edges[rc_edge_id] = rc_edge
                    for overlapped_edge, overlap in edge_overlaps[rc_edge_id].items():
                        dict_edges[edge_id].overlaps.append((edge_id_to_name(overlapped_edge), overlapped_edge, overlap))

            if record_type != 'L' and record_type != 'E':
                continue
            if record_type == 'L':
                _, from_name, from_orient, to_name, to_orient = line.split()[:5]
            else:
                # E       *       2+      65397+  21      68$     0       47      47M
                from_name, to_name = line.split()[2], line.split()[3]
                from_orient, to_orient = from_name[-1], to_name[-1]
                from_name, to_name = from_name[:-1], to_name[:-1]
            edge1 = get_edge_agv_id(get_edge_num(from_name))
            edge2 = get_edge_agv_id(get_edge_num(to_name))
            if from_orient == '-': edge1 = get_match_edge_id(edge1)
            if to_orient == '-': edge2 = get_match_edge_id(edge2)
            overlap = 0
            overlap_operations = re.split('(\d+)', line.split()[-1].strip())
            for i in range(0, len(overlap_operations) - 1, 1):
                if not overlap_operations[i]:
                    continue
                if overlap_operations[i+1] == 'M' or overlap_operations[i+1] == 'I':
                    overlap += int(overlap_operations[i])
            links.append((from_name, from_orient, to_name, to_orient, overlap))
            if overlap:
                edge_overlaps[edge1][edge2] = overlap
                edge_overlaps[edge2][edge1] = overlap

    ### gfa retains only canonical links
    for link in links:
        from_name, from_orient, to_name, to_orient, overlap = link
        edge1 = get_edge_agv_id(get_edge_num(from_name))
        edge2 = get_edge_agv_id(get_edge_num(to_name))
        if from_orient == '-': edge1 = get_match_edge_id(edge1)
        if to_orient == '-': edge2 = get_match_edge_id(edge2)
        #if edge1 != edge2:
        predecessors[edge2].append(edge1)
        successors[edge1].append(edge2)
        g.add_edge(edge1, edge2)
        if is_spades(assembler) or is_abyss(assembler):
            edge1, edge2 = get_match_edge_id(edge2), get_match_edge_id(edge1)
            if edge1 != edge2:
                predecessors[edge2].append(edge1)
                successors[edge1].append(edge2)
            g.add_edge(edge1, edge2)

    if assembler == "canu" and input_dirpath:
        dict_edges = parse_canu_unitigs_info(input_dirpath, dict_edges)
    dict_edges = construct_graph(dict_edges, predecessors, successors)
    print("Finish parsing.")
    return dict_edges


def calculate_multiplicities(dict_edges):
    ## calculate inferred edge multiplicity as edge coverage divided by median coverage
    median_cov = calculate_median_cov(dict_edges)
    for name in dict_edges:
        multiplicity = dict_edges[name].cov / median_cov
        dict_edges[name].multiplicity = 1 if multiplicity <= 1.75 else round(multiplicity)
        if dict_edges[name].multiplicity > 1:
            dict_edges[name].repetitive = True
    return dict_edges


def construct_graph(dict_edges, predecessors, successors):
    dict_edges = calculate_multiplicities(dict_edges)

    # if we have only links between sequences
    # we need to construct graph based on this information
    node_id = 1
    graph = defaultdict(set)
    adj_matrix = defaultdict(list)
    for edge_id in dict_edges.keys():
        start_node = None
        is_edge_repetitive = dict_edges[edge_id].repetitive
        for prev_e in predecessors[edge_id]:
            if prev_e in dict_edges:
                start_node = dict_edges[prev_e].end or start_node
            j = 0
            while start_node is None and j < len(successors[prev_e]):
                next_e = successors[prev_e][j]
                if next_e in dict_edges:
                    start_node = dict_edges[next_e].start or start_node
                j += 1
            if start_node is not None:
                break
        if not start_node:
            start_node = node_id
            if edge_id not in successors[edge_id]:
                node_id += 1
        end_node = None
        for next_e in successors[edge_id]:
            if next_e in dict_edges:
                end_node = dict_edges[next_e].start or end_node
            j = 0
            while end_node is None and j < len(predecessors[next_e]):
                prev_e = predecessors[next_e][j]
                if prev_e in dict_edges:
                    end_node = dict_edges[prev_e].end or end_node
                j += 1
            if end_node is not None:
                break
        if not end_node:
            end_node = node_id
            node_id += 1
        if is_edge_repetitive:
            adj_matrix[start_node].append(edge_id)
            adj_matrix[end_node].append(edge_id)
        dict_edges[edge_id].start = start_node
        dict_edges[edge_id].end = end_node
    for node_id, edges in adj_matrix.items():
        for i, edge_id in enumerate(edges):
            for j, adj_edge_id in enumerate(edges):
                if i != j:
                    graph[edge_id].add(adj_edge_id)

    ### color each cluster of repeat edges in one color
    colored_edges = set()
    color_idx = 0
    for edge_id in dict_edges.keys():
        if not dict_edges[edge_id].repetitive:
            continue
        if edge_id in colored_edges:
            continue
        edges = dfs_color(graph, edge_id)
        color = repeat_colors[color_idx % len(repeat_colors)]
        # color forward and reverse complement edges in one color
        match_edge_id = edge_id.replace('rc', 'e') if edge_id.startswith('rc') else edge_id.replace('e', 'rc')
        if match_edge_id in dict_edges:
            edges |= dfs_color(graph, match_edge_id)
        for e in edges:
            dict_edges[e].color = color
            colored_edges.add(e)
        color_idx += 1
    return dict_edges


def dfs_color(graph, start, visited=None):
    if visited is None:
        visited = set()
    visited.add(start)
    stack = [iter(graph[start] - visited)]
    while stack:
        try:
            child = next(stack[-1])
            if not child in visited:
                visited.add(child)
                stack.append(iter(graph[child] - visited))
        except StopIteration:
            stack.pop()
    return visited

