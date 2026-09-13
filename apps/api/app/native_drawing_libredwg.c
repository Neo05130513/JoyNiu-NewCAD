/* SPDX-License-Identifier: GPL-3.0-or-later
 * Standalone LibreDWG compatibility adapter, never linked into the API process.
 * Restores only numeric fields that LibreDWG 0.14's DXF importer misreads.
 * Source geometry and strings are retained; Python validates the entire result.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <dwg.h>

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts("JoyNiu native DWG compatibility adapter / LibreDWG");
        return 0;
    }
    if (argc != 4) return 2;
    Dwg_Data drawing = {0};
    int status = dwg_read_file(argv[1], &drawing);
    if (status >= DWG_ERR_CRITICAL) return 3;
    FILE *patch = fopen(argv[2], "r");
    if (!patch) { dwg_free(&drawing); return 4; }
    char line[1024], kind[12], extra;
    unsigned long long handle;
    double a, b, c, d, e;
    unsigned count = 0;
    while (fgets(line, sizeof(line), patch)) {
        if (++count > 500000 || sscanf(line, "%11s %llx %lf %lf %lf %lf %lf %c",
                kind, &handle, &a, &b, &c, &d, &e, &extra) != 7
                || !isfinite(a) || !isfinite(b) || !isfinite(c) || !isfinite(d) || !isfinite(e)) {
            status = 5; goto done;
        }
        Dwg_Object *object = dwg_resolve_handle_silent(&drawing, handle);
        if (!object) { status = 6; goto done; }
        if (!strcmp(kind, "DIMSTYLE") && object->fixedtype == DWG_TYPE_DIMSTYLE) {
            Dwg_Object_DIMSTYLE *style = object->tio.object->tio.DIMSTYLE;
            style->DIMCEN = a; style->DIMTOFL = (unsigned char)b;
            style->DIMTAD = (short)c; style->DIMATFIT = (short)d;
            continue;
        }
        if (!strcmp(kind, "BLOCK") && object->fixedtype == DWG_TYPE_BLOCK_HEADER) {
            Dwg_Object_BLOCK_HEADER *block = object->tio.object->tio.BLOCK_HEADER;
            unsigned flags = (unsigned)a;
            block->anonymous = !!(flags & 1); block->hasattrs = !!(flags & 2);
            block->blkisxref = !!(flags & 4); block->xrefoverlaid = !!(flags & 8);
            block->base_pt.x = b; block->base_pt.y = c; block->base_pt.z = d;
            continue;
        }
        if (object->supertype != DWG_SUPERTYPE_ENTITY) { status = 6; goto done; }
        if (!strcmp(kind, "EEDGROUP")) {
            Dwg_Object_Entity *entity = object->tio.entity;
            if (a < 0 || a != floor(a) || a >= entity->num_eed || c != entity->num_eed
                    || b < 1 || b != floor(b)) { status = 12; goto done; }
            if (a == 0) {
                for (unsigned i = 0; i < entity->num_eed; ++i) {
                    free(entity->eed[i].raw); entity->eed[i].raw = NULL;
                    entity->eed[i].size = 0;
                }
            }
            Dwg_Eed *entry = &entity->eed[(unsigned)a];
            entry->size = 1; /* Encoder recalculates sizes from decoded data. */
            entry->handle.code = 5; entry->handle.value = (unsigned long long)b;
            entry->handle.size = 0;
            for (unsigned long long v = entry->handle.value; v; v >>= 8) ++entry->handle.size;
            continue;
        }
        if (!strcmp(kind, "MTEXT") && object->fixedtype == DWG_TYPE_MTEXT) {
            Dwg_Entity_MTEXT *text = object->tio.entity->tio.MTEXT;
            text->text_height = a; text->rect_width = b;
            text->linespace_style = (short)c; text->linespace_factor = d;
            text->flow_dir = (short)e;
        } else if (!strcmp(kind, "TEXTALIGN") && object->fixedtype == DWG_TYPE_TEXT) {
            Dwg_Entity_TEXT *text = object->tio.entity->tio.TEXT;
            text->alignment_pt.x = a; text->alignment_pt.y = b;
            text->horiz_alignment = (short)c; text->vert_alignment = (short)d;
            text->dataflags &= ~2;
        } else if (!strcmp(kind, "INSERT") && object->fixedtype == DWG_TYPE_INSERT) {
            Dwg_Entity_INSERT *insert = object->tio.entity->tio.INSERT;
            insert->scale.x = a; insert->scale.y = b; insert->scale.z = c;
            insert->scale_flag = 0;
        } else if (!strcmp(kind, "LWWIDTH") && object->fixedtype == DWG_TYPE_LWPOLYLINE) {
            Dwg_Entity_LWPOLYLINE *polyline = object->tio.entity->tio.LWPOLYLINE;
            if (a < 0 || a != floor(a) || a >= polyline->num_points || polyline->num_points > 500000) { status = 10; goto done; }
            if (polyline->num_widths != polyline->num_points) {
                free(polyline->widths);
                polyline->widths = calloc(polyline->num_points, sizeof(Dwg_LWPOLYLINE_width));
                if (!polyline->widths) { status = 11; goto done; }
                polyline->num_widths = polyline->num_points;
            }
            polyline->flag |= 32;
            polyline->widths[(unsigned)a].start = b;
            polyline->widths[(unsigned)a].end = c;
        } else if (!strcmp(kind, "DIMENSION") && object->fixedtype >= DWG_TYPE_DIMENSION_ORDINATE
                    && object->fixedtype <= DWG_TYPE_DIMENSION_DIAMETER) {
            Dwg_DIMENSION_common *dimension = object->tio.entity->tio.DIMENSION_common;
            dimension->lspace_style = (short)a; dimension->lspace_factor = b;
            if (object->fixedtype == DWG_TYPE_DIMENSION_LINEAR)
                object->tio.entity->tio.DIMENSION_LINEAR->dim_rotation = c;
        } else { status = 7; goto done; }
    }
    if (ferror(patch)) { status = 8; goto done; }
    status = dwg_write_file(argv[3], &drawing) >= DWG_ERR_CRITICAL ? 9 : 0;
done:
    fclose(patch);
    dwg_free(&drawing);
    return status;
}
