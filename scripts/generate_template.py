#!/usr/bin/env python

"""
This script generates both the Elasticsearch template file (*.template.json)
and the Kibana index pattern setting file (*.index-pattern.json) from the
fields.yml file and skeleton.json.
The script is built upon the similar script from libbeats.

Example usage:

   python generate_template.py <template_definition> <namespaces_directory>
"""

import argparse
import yaml
import json
import sys
import io

def object_types_to_template(template_definition, output, output_index_pattern, namespaces_dir):
    """
    Assemble objects for the particular template.
    """

    if template_definition is None:
        print("template.yml is empty. Cannot generate template.")
        return

    if 'skeleton_path' not in template_definition or\
       'namespaces' not in template_definition:
        print("skeleton_path is not defined. Cannot generate template.")
        return

    # Load skeleton of the template
    with open(template_definition['skeleton_path'], 'r') as f:
        skeleton = yaml.load(f)

    if 'skeleton_index_pattern_path' not in template_definition:
        print("skeleton_index_pattern_path is not defined. Cannot generate template.")
        return

    # Load skeleton of the template
    with open(template_definition['skeleton_index_pattern_path'], 'r') as f:
        skeleton_index_pattern = yaml.load(f)

    # Load object_type files
    with open(namespaces_dir + '/_default_.yml', 'r') as f:
        default_mapping_yml = yaml.load(f)
    default_mapping = default_mapping_yml['_default_']

    for ns_file in template_definition['namespaces']:
        with open(namespaces_dir + ns_file, 'r') as f:
            cur_ns_yml = yaml.load(f)
        if 'namespace' not in cur_ns_yml:
            print("namespace section is absent in file {0}".format(ns_file))
            return

        default_mapping['fields'].append(cur_ns_yml['namespace'])

    # traverse group section returns a hash - the hash will contain a field
    # called 'properties' which contains the field definitions for the fields
    # in the group, and other settings applicable to groups such as
    # include_in_all, etc.
    skeleton['mappings']['_default_'].update(traverse_group_section(
        default_mapping, default_mapping_yml['field_defaults'], process_leaf, True))

    add_type_version(default_mapping_yml["version"],
                     skeleton['mappings']['_default_'])

    add_index_pattern(
        template_definition['elasticsearch_template']['index_pattern'],
        skeleton)
    add_index_order(template_definition['elasticsearch_template']['order'],
                    skeleton)
    for field in ['_source', '_all', 'include_in_all']:
        if field in template_definition['elasticsearch_template']:
            skeleton['mappings']['_default_'][field] = template_definition['elasticsearch_template'][field]

    json.dump(
        skeleton, output, indent=2, separators=(',', ': '), sort_keys=True)
    output.write('\n')

    # index pattern stuff
    time_field_name = "time"
    for ii in default_mapping["fields"]:
        if ii['type'] == 'date':
            time_field_name = ii['name']
            break
    skeleton_index_pattern["timeFieldName"] = time_field_name
    skeleton_index_pattern["description"] = skeleton_index_pattern["description"].replace("<the_index_type>", template_definition['elasticsearch_template']['index_pattern'])
    # get fields
    index_pattern_fields = (traverse_group_section_index_pattern(
        default_mapping, default_mapping_yml['field_defaults'], process_leaf_index_pattern, None, True))
    skeleton_index_pattern["fields"] = json.dumps(index_pattern_fields)
    json.dump(
        skeleton_index_pattern, output_index_pattern, indent=2, separators=(',', ': '), sort_keys=True)
    output_index_pattern.write('\n')


def add_mapping_to_skeleton(map_type, skeleton):
    """Add mapping type to the skeleton by cloning '_default_' section.
    Args:
        map_type(str): name of the document type to add
        skeleton(dict): skeleton to update
    """
    if map_type != '_default_':
        skeleton['mappings'][map_type] = skeleton['mappings'][
            '_default_'].copy()
        del skeleton['mappings'][map_type]['dynamic_templates']


def add_index_template_fields(rec):
    doc_fields = ["description", "example", "path", "name"]
    ret = {}
    for field in rec:
        if field in doc_fields: continue
        if field == 'fields': continue
        ret[field] = rec[field]
    return ret


def traverse_group_section(group, leaf_defaults, leaf_handler, toplevel=False):
    """
    Traverse the sections tree and fill in the properties
    map.
    Args:
        group(dict): field of type group, that has multiple subfields under
    'fields' key, and possibly other parameters we want to represent in the
    index template.  Parameters specified in the field will override
    parameters specified in defaults.
        defaults(dict): dict with the defaults for all fields
    Returns:
        dict containing the key 'properties' containing the definitions
        of the fields in the group, plus any other group specific
        parameters
    """
    field = add_index_template_fields(group)
    if toplevel or not 'name' in group:
        ret = field
    else:
        ret = {group['name']: field}
    if group['type'] == 'group':
        fieldskey = 'properties'
        del field['type']
    else:
        fieldskey = 'fields'
        leaf_handler(field, leaf_defaults)
    if 'fields' in group:
        field[fieldskey] = {}
        for subfield in group['fields']:
            rec = traverse_group_section(subfield, leaf_defaults, leaf_handler)
            if rec:
                field[fieldskey].update(rec)
    elif not field:
        ret = None

    return ret


def process_leaf(field, defaults):
    """Process field that is not a group. Fill the template copy with the actual
    data.
    Args:
        field(dict): contents of the field.
        defaults(dict): default values.
    Returns:
        dict corresponding to the data in the particular field.
    """
    other_known_types = ["string", "date", "ip", "integer", "long",
                         "boolean", "short", "byte"]

    for key in defaults.keys():
        if key not in field:
            field[key] = defaults[key]

    if field.get("type") in ["object", "nested"]:
        fieldtype = field.get("type")
        if "object_struct" in field:
            # just replace field with contents of 'object_struct'
            tmp = field['object_struct'].copy()
            field.clear()
            field.update(tmp)
        else:
            # just clear the field
            field.clear()
        field['type'] = fieldtype
    elif field.get("type") == "float":
        field["doc_values"] = "true"
    elif not field.get("type") in other_known_types:
        print("Unknown field. Skipped adding field {}".format(field))


def traverse_group_section_index_pattern(group, defaults, leaf_handler, groupname=None, toplevel=False):
    """
    Traverse the sections tree and fill in the index pattern fields
    map.
    Args:
        group(dict): field of type group, that has multiple subfields under
    'fields' key.
        defaults(dict): dict with the defaults for all fields
    Returns:
        array of field definitions.
    """
    fields = []

    # print "Trying to fill section properties of section %s" % (group)
    try:
        for field in group["fields"]:
            if groupname:
                subgroupname = groupname + "." + group["name"]
            elif toplevel:
                subgroupname = None
            else:
                subgroupname = group.get("name", None)
            if field.get("type") == "group" or "fields" in field:
                if not field.get("type") == "group": # assume leaf
                    out_field = leaf_handler(field, defaults, subgroupname)
                    fields.append(out_field)
                more_fields = traverse_group_section_index_pattern(field, defaults, leaf_handler, subgroupname)
                fields.extend(more_fields)
            else:
                out_field = leaf_handler(field, defaults, subgroupname)
                fields.append(out_field)
    except KeyError:
        print("Skipping empty section {}".format(group))
    # print "Section filled with properties: %s" % (properties)
    return fields


def process_leaf_index_pattern(field, defaults, groupname):
    """Process field that is not a group. Fill the template copy with the actual
    data.
    Args:
        field(dict): contents of the field.
        defaults(dict): default values.
        groupname(string): name of group this field belongs to e.g. "systemd.u"
    Returns:
        dict corresponding to the data in the particular field.
    """
    if groupname:
        fieldname = groupname + "." + field["name"]
    else:
        fieldname = field["name"]
    # Kibana field types:
    # https://github.com/elastic/kibana/blob/master/src/ui/public/index_patterns/_field_types.js
    if field.get("type") in ["string", "date", "ip", "boolean"]:
        fieldtype = field.get("type")
    elif field.get("type") in ["integer", "long", "short", "byte", "float"]:
        fieldtype = "number"
    elif field.get("type") == "object":
        if "geo_point" == field.get("object_struct", {}).get("properties", {}).get("location", {}).get("type", ''):
            fieldtype = "geo_point"
        else:
            fieldtype = "string"
    elif field.get("type") == "nested":
        fieldtype = "string"
    else:
        print("Unknown field type. Skipped adding field {}".format(field))
    analyzed = field.get("index", "") == "analyzed"
    res = {
        "name": fieldname,
        "type": fieldtype,
        "count": 0,
        "scripted": False,
        "indexed": True,
        "analyzed": analyzed,
        "doc_values": field.get("doc_values", True)
    }
    return res


def add_type_version(version, obj_type):
    """replaces <version> placeholder in the template(index name and _meta)
    with the actual version number
    Args:
        version(str): version of the object
        obj_type(dict): dict of object_type where to replace the version
    """
    obj_type["_meta"]["version"] = version
    # template["template"] = template["template"].replace("<version>", version)


def add_index_pattern(pattern, template_skeleton):
    """Adds index pattern to the template, overwriting the previous index pattern
    Args:
        pattern(list): list of str, index patterns to be used in the template.
        template_skeleton(dict): template to operate upon.
    """
    template_skeleton['template'] = pattern


def add_index_order(order, template_skeleton):
    """Adds order to the template, overwriting the existing order value.
    Args:
        order(int): order value
        template_skeleton(dict): template to operate upon
    """
    template_skeleton['order'] = order

def object_types_to_asciidoc(template_definition, output, namespaces_dir):
    """
    Assemble asciidoc for the particular template.
    """

    if template_definition is None:
        print('template.yml is empty. Cannot generate asciidoc.')
        return

    if 'namespaces' not in template_definition:
        print('namespaces not defined. Cannot generate asciidoc.')
        return

    if 'skeleton_index_pattern_path' not in template_definition:
        print('skeleton_index_pattern_path is not defined. Cannot generate template.')
        return

    # Load skeleton of the template
    with open(template_definition['skeleton_index_pattern_path'], 'r') as f:
        skeleton_index_pattern = yaml.load(f)

    # Load object_type files
    with open(namespaces_dir + '/_default_.yml', 'r') as f:
        default_mapping_yml = yaml.load(f)
    sections = [default_mapping_yml['_default_']]

    for ns_file in template_definition['namespaces']:
        with open(namespaces_dir + ns_file, 'r') as f:
            cur_ns_yml = yaml.load(f)
        if 'namespace' not in cur_ns_yml:
            print('namespace section is absent in file {0}'.format(ns_file))
            return

        sections.append(cur_ns_yml['namespace'])

    dict = {'product': template_definition['elasticsearch_template']['name']}

    output.write(u"""
////
This file is generated! See scripts/generate_template.py --docs
////

[[exported-fields]]
== Exported Fields

These are the fields exported by the logging system and available for searching
from Elasticsearch and Kibana.  Use the full, dotted field name when searching.
For example, for an Elasticsearch /_search URL, to look for a Kubernetes pod name,
use `/_search/q=kubernetes.pod_name:name-of-my-pod`
This document describes fields that may not be present in your logging store.
Not all of these fields are present in every record.
The fields are grouped in the following categories:

""".format(**dict))

    # Generate table of contents
    for doc in sections:
        output.write(u'* <<exported-fields-{}>>\n'.format(doc['name']))
    output.write(u'\n')

    for field in sections:
#        print('Working on section: {}'.format(field))

        document_fields(output, field, [])

def document_fields(output, section, hier_path=[]):

    if section['name'] == 'Default':
        str_path = u''
        secname = 'Top Level'
        linkname = section['name']
    else:
        hier_path.append(section['name'])
        str_path = '.'.join(hier_path)
        secname = str_path
        linkname = str_path

    output.write(u'[[exported-fields-{}]]\n'.format(linkname))
    output.write(u'=== {} Fields\n\n'.format(secname))

    if 'description' in section:
        output.write(u'{}\n\n'.format(section['description']))

    if 'fields' not in section or not section['fields']:
        return

    output.write(u'\n')
    for field in section['fields']:

        if 'type' in field and field['type'] == 'group':
            document_fields(output, field, hier_path[:])
        else:
            document_field(output, field, str_path)


def document_field(output, field, str_path):

    if len(str_path) == 0:
        path = field['name']
    else:
        path = str_path + u'.' + field['name']

    output.write(u'==== {}\n\n'.format(path))

    if 'type' in field:
        output.write(u'type: {}\n\n'.format(field['type']))
    if 'example' in field:
        output.write(u'example: {}\n\n'.format(field['example']))
    if 'format' in field:
        output.write(u'format: {}\n\n'.format(field['format']))
    if 'required' in field:
        output.write(u'required: {}\n\n'.format(field['required']))

    if 'description' in field:
        output.write(u'{}\n\n'.format(field['description']))


def parse_args():
    p = argparse.ArgumentParser()

    p.add_argument('template_definition',
                   help='Path to input template')
    p.add_argument('namespaces_dir',
                   help='Path to directory with namespace definitions')
    p.add_argument('--docs', action='store_true', default=False,
                   help='Generate field documentation')

    return p.parse_args()

if __name__ == '__main__':
    args = parse_args()

    with open(args.template_definition, 'r') as input_template:
        template_definition = yaml.load(input_template)

    if args.docs:
        with io.open('{0[elasticsearch_template][name]}.asciidoc'.format(
                template_definition), mode='w', encoding='utf8') as output:
            object_types_to_asciidoc(template_definition, output,
                                     args.namespaces_dir)
        sys.exit()

    with open('{0[elasticsearch_template][name]}.template.json'.format(
            template_definition), 'w') as output:
        with open('{0[elasticsearch_template][name]}.index-pattern.json'.format(
                template_definition), 'w') as output_index_pattern:
            object_types_to_template(template_definition, output, output_index_pattern, args.namespaces_dir)
