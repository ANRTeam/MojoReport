import re, os, io, logging
from collections import Counter
from datetime import datetime
import pdfplumber
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import streamlit as st
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, PageBreak, KeepTogether
)

logging.getLogger("pdfminer").setLevel(logging.ERROR)

# ── Helpers ────────────────────────────────────────────────────────────────
def parse_hms(t):
    try:
        h, m, s = t.strip().split(':')
        return int(h)*60 + int(m) + int(s)/60
    except:
        return 0.0

def fmt_dial(mins):
    h, m = int(mins // 60), int(mins % 60)
    if h > 0 and m > 0: return f"{h}h {m}m"
    if h > 0:           return f"{h}h"
    return f"{m}m"

def fmt_talk(mins):
    if mins == 0: return "0m"
    h, m = int(mins // 60), int(mins % 60)
    s = int(round((mins % 1) * 60))
    if h > 0: return f"{h}h {m}m"
    if m > 0: return f"{m}m {s}s" if s > 0 else f"{m}m"
    return f"{s}s"

def clean_agent_name(name):
    name = re.sub(r'\s+Parks?$', ' Park', name.strip(), flags=re.IGNORECASE)
    return name.title()

def clean_result(r):
    r = re.sub(r'\s+', ' ', r).strip()
    if re.match(r'(?i)^Answering$', r): return 'Answering Machine'
    if re.match(r'(?i)^Machine\s+No\s+Answer$', r): return 'No Answer'
    if re.match(r'(?i)^No\s+Ariswer$', r): return 'No Answer'
    if re.match(r'(?i)^Drop$', r): return 'Drop Voicemail'
    if re.match(r'(?i)^Voicemail\s+Disconnected$', r): return 'Disconnected'
    r = re.sub(r'(?i)Disconnecte\s*d', 'Disconnected', r)
    return r.title() if r else 'Unknown'

def clean_list(l):
    l = re.sub(r'\s+', ' ', l).strip()
    return l if l else 'Unknown'

# ── Parser ─────────────────────────────────────────────────────────────────
def extract_session_records(file_bytes):
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)

    lines = [l.strip() for l in full_text.split('\n') if l.strip()]
    
    # ✅ FIX: Extract results from FINAL TOTAL section ONLY
    result_counts = extract_total_results(lines)
    
    # Extract agent info and time data
    agent_name = 'Unknown'
    total_appts = 0
    total_leads = 0
    dial_time_str = '0:00:00'
    total_dial_mins = 0.0
    total_talk_mins = 0.0

    # Look for the agent total line with aggregated stats
    for line in lines:
        m = re.match(r'^Total\s+(.+?)\s+-\s+-\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d:]+)\s+([\d:]+)', line)
        if m:
            agent_name      = clean_agent_name(m.group(1))
            total_appts     = int(m.group(3))
            total_leads     = int(m.group(4))
            dial_time_str   = m.group(5)
            total_dial_mins = parse_hms(dial_time_str)
            total_talk_mins = parse_hms(m.group(6))
            break
            
    # Fallback: extract from TOTAL line with times
    if total_dial_mins == 0.0:
        for line in lines:
            if line.startswith('TOTAL') and re.search(r'([\d]{2}:[\d]{2}:[\d]{2})', line):
                matches = re.findall(r'([\d]{2}:[\d]{2}:[\d]{2})', line)
                if len(matches) >= 2:
                    total_talk_mins = parse_hms(matches[-2])
                    total_dial_mins = parse_hms(matches[-1])
                elif len(matches) == 1:
                    total_dial_mins = parse_hms(matches[-1])

    # Fallback: extract agent name from total line
    if agent_name == 'Unknown':
        for line in lines:
            if 'total' in line.lower() and not line.lower().startswith('total calls'):
                parts = line.split()
                if len(parts) > 1 and parts[0].lower() == 'total':
                    agent_name = clean_agent_name(' '.join(parts[1:]).split('-')[0])
                    break

    # Extract session details for per-date breakdown
    sess_pat = re.compile(
        r'(\d{1,2}/\d{1,2}/\d{4})\s+'                  
        r'([A-Za-z\s]+?)\s+'                           
        r'(Power Dial(?:er)?|C2C Session|Multi-Line|Single Line|Preview|Dialer)\s+' 
        r'(.+?)\s+'                                    
        r'(\d{1,4})(?:\s+of)?\s+'                      
        r'(\d+)\s+'                                    
        r'(?:[\d:]+\s+)?'                              
        r'(\d+)\s+'                                    
        r'([\d:]+)\s+'                                 
        r'([\d:]+)\s+'                                 
        r'([\d:]+)\s+'                                 
        r'([\d:]+\s*[AP]M)\s+'                         
        r'([\d:]+\s*[AP]M)',                           
        flags=re.IGNORECASE
    )
    
    sessions = []
    date_counts = Counter()
    list_counts = Counter()
    
    for line in lines:
        m = sess_pat.search(line)
        if m:
            date  = m.group(1)
            lst = clean_list(m.group(4).strip())
            calls = int(m.group(5))
            sessions.append({
                'date': date, 'type': m.group(3), 'list': lst, 'calls': calls,
                'dial_mins': parse_hms(m.group(8))
            })
            date_counts[date] += calls
            list_counts[lst]  += calls

    # Try to get list names from Group/List Dialed field
    true_lists_matches = re.findall(r'Group/List\s*Dialed:\s*([^\n]+)', full_text, re.IGNORECASE)
    true_lists = []
    for m in true_lists_matches:
        m = clean_list(m)
        if m and m.lower() != 'unknown' and m != '-':
            true_lists.append(m)
    true_lists = list(dict.fromkeys(true_lists))
    
    if true_lists:
        parsed_keys = list(list_counts.keys())
        if len(true_lists) == len(parsed_keys):
            new_list_counts = Counter()
            for true_name, old_key in zip(true_lists, parsed_keys):
                new_list_counts[true_name] = list_counts[old_key]
            list_counts = new_list_counts
        elif len(true_lists) == 1:
            list_counts = Counter({true_lists[0]: sum(list_counts.values())})

    if sum(list_counts.values()) == 0 and sum(result_counts.values()) > 0 and true_lists:
        if len(true_lists) == 1:
            list_counts[true_lists[0]] = sum(result_counts.values())
        else:
            split = sum(result_counts.values()) // len(true_lists)
            for lst in true_lists: 
                list_counts[lst] = split
            list_counts[true_lists[0]] += sum(result_counts.values()) % len(true_lists)

    true_total_calls = sum(result_counts.values())

    return {
        'agent_name':    agent_name,
        'sessions':      sessions,
        'result_counts': result_counts,
        'list_counts':   list_counts,
        'date_counts':   date_counts,
        'total_calls':   true_total_calls,
        'total_appts':   total_appts,
        'total_leads':   total_leads,
        'dial_mins':     total_dial_mins,
        'dial_str':      fmt_dial(total_dial_mins),
        'talk_mins':     total_talk_mins,
        'talk_str':      fmt_talk(total_talk_mins),
    }


# ✅ NEW FUNCTION: Extract results from the FINAL TOTAL section only
def extract_total_results(lines):
    """
    Parse the final TOTAL section to get accurate result counts.
    The TOTAL section appears near the end and has all aggregated results.
    """
    result_counts = Counter()
    
    # Find where the final TOTAL section starts
    # We want the TOTAL section that comes after "Result Total Calls"
    in_total_section = False
    total_section_lines = []
    
    for i, line in enumerate(lines):
        # Detect start of a TOTAL results section
        if re.match(r'^Result\s+Total\s+Calls', line, re.IGNORECASE):
            in_total_section = True
            continue
        
        # Collect lines until we hit the next "TOTAL" summary line or "Appts / Leads"
        if in_total_section:
            if re.match(r'^TOTAL\s+\d+', line, re.IGNORECASE):
                # This is the summary line, stop collecting
                in_total_section = False
                continue
            if re.match(r'^Appts\s*/\s*Leads', line, re.IGNORECASE):
                # End of results section
                in_total_section = False
                continue
            
            # Parse result lines: "Result Name     Count  Time"
            m = re.match(r'^([A-Za-z\s/&_-]+?)\s+(\d+)\s+[\d:]+', line)
            if m:
                result_name = m.group(1).strip()
                count = int(m.group(2))
                
                # Skip header rows and invalid entries
                if result_name.lower() not in ('result', 'talk time', 'dial time', 'total calls'):
                    clean_name = clean_result(result_name)
                    if clean_name and clean_name.lower() != 'unknown':
                        result_counts[clean_name] = count
    
    # ✅ If we didn't find results with the above method, use the last TOTAL block
    if sum(result_counts.values()) == 0:
        result_counts = extract_total_results_fallback(lines)
    
    return result_counts


def extract_total_results_fallback(lines):
    """
    Fallback: Find the LAST "TOTAL" summary that contains all aggregated data.
    This is more robust for PDFs with multiple sessions.
    """
    result_counts = Counter()
    
    # Find all "TOTAL" lines with numeric data
    total_blocks = []
    for i, line in enumerate(lines):
        if re.match(r'^TOTAL\s+\d+', line, re.IGNORECASE):
            # Start of a TOTAL block - collect preceding result lines
            block = []
            j = i - 1
            while j >= 0:
                prev_line = lines[j]
                # Stop at result header or another TOTAL
                if re.match(r'^Result\s+Total\s+Calls', prev_line, re.IGNORECASE):
                    j += 1
                    break
                if re.match(r'^TOTAL', prev_line, re.IGNORECASE) and j < i - 1:
                    j += 1
                    break
                block.insert(0, prev_line)
                j -= 1
            total_blocks.append((i, block))
    
    # Use the LAST total block (most comprehensive aggregation)
    if total_blocks:
        _, block = total_blocks[-1]
        for line in block:
            m = re.match(r'^([A-Za-z\s/&_-]+?)\s+(\d+)\s+[\d:]+', line)
            if m:
                result_name = m.group(1).strip()
                count = int(m.group(2))
                
                # Skip headers
                if result_name.lower() not in ('result', 'talk time', 'dial time'):
                    clean_name = clean_result(result_name)
                    if clean_name and clean_name.lower() != 'unknown':
                        result_counts[clean_name] = count
    
    return result_counts


# ── Colours ────────────────────────────────────────────────────────────────
BLUE       = colors.HexColor('#185FA5')
BLUE_LIGHT = colors.HexColor('#E6F1FB')
TEAL       = colors.HexColor('#0F6E56')
TEAL_LIGHT = colors.HexColor('#E1F5EE')
GRAY_TEXT  = colors.HexColor('#5F5E5A')
GRAY_MED   = colors.HexColor('#888780')
GRAY_LIGHT = colors.HexColor('#F1EFE8')
BORDER     = colors.HexColor('#D3D1C7')
RED_BG     = colors.HexColor('#FCEBEB')
RED_TEXT   = colors.HexColor('#A32D2D')
GREEN_BG   = colors.HexColor('#EAF3DE')
GREEN_TEXT = colors.HexColor('#27500A')
BLACK      = colors.HexColor('#2C2C2A')
WHITE      = colors.white

DONUT_PALETTE = ['#185FA5','#0F6E56','#C2410C','#7C3AED','#B45309','#0E7490','#BE185D','#4D7C0F','#64748B']

# ── Styles ─────────────────────────────────────────────────────────────────
def build_styles():
    s = {}
    s['title']    = ParagraphStyle('title',    fontName='Helvetica-Bold', fontSize=20, textColor=BLACK,     spaceAfter=2,   leading=24)
    s['subtitle'] = ParagraphStyle('subtitle', fontName='Helvetica',      fontSize=10, textColor=GRAY_MED,  spaceAfter=0,   leading=14)
    s['section']  = ParagraphStyle('section',  fontName='Helvetica-Bold', fontSize=11, textColor=BLACK,     spaceBefore=14, spaceAfter=6, leading=14)
    s['body']     = ParagraphStyle('body',     fontName='Helvetica',      fontSize=9,  textColor=GRAY_TEXT, leading=14,     spaceAfter=4)
    s['small']    = ParagraphStyle('small',    fontName='Helvetica',      fontSize=8,  textColor=GRAY_MED,  leading=11)
    s['team_h']   = ParagraphStyle('team_h',   fontName='Helvetica-Bold', fontSize=16, textColor=BLACK,     spaceAfter=2,   leading=20)
    return s

# ── Graphs ─────────────────────────────────────────────────────────────────
def buf_leaderboard(teams_data):
    for t in teams_data:
        t['val_time']     = t['stats']['dial_mins']
        t['val_calls']    = t['stats']['total_calls']
        t['val_voicemail'] = sum(v for k,v in t['stats']['result_counts'].items() if 'voicemail' in k.lower())
        t['val_no_answer'] = sum(v for k,v in t['stats']['result_counts'].items() if 'no answer' in k.lower() or 'machine no answer' in k.lower())
        t['val_other']     = max(0, t['val_calls'] - (t['val_voicemail'] + t['val_no_answer']))
        t['val_appts']     = t['stats']['total_appts']
        t['val_leads']     = t['stats'].get('total_leads', 0)

    totals = {
        'time': sum(t['val_time'] for t in teams_data), 'calls': sum(t['val_calls'] for t in teams_data),
        'voicemail': sum(t['val_voicemail'] for t in teams_data), 'no_answer': sum(t['val_no_answer'] for t in teams_data),
        'other': sum(t['val_other'] for t in teams_data), 'appts': sum(t['val_appts'] for t in teams_data),
        'leads': sum(t['val_leads'] for t in teams_data),
    }

    metrics_info = [
        {'title': 'DIALING TIME', 'key': 'time', 'col_title': 'TIME'},
        {'title': 'CALLS', 'key': 'calls', 'col_title': 'CALLS'},
        {'title': 'DROP VOICEMAIL', 'key': 'voicemail', 'col_title': 'VOICEMAIL'},
        {'title': 'NO ANSWER', 'key': 'no_answer', 'col_title': 'NO ANSWER'},
        {'title': 'OTHER', 'key': 'other', 'col_title': 'OTHER'},
        {'title': 'APPOINTMENTS', 'key': 'appts', 'col_title': 'APPTS'},
        {'title': 'LEADS', 'key': 'leads', 'col_title': 'LEADS'},
    ]

    fig = plt.figure(figsize=(16, 6.5), facecolor='#FFFFFF')
    gs  = fig.add_gridspec(2, 7, height_ratios=[1.0, 1.6], wspace=0.25, hspace=0.22)

    def fmt_val(v, key):
        if key == 'time':
            if not v: return '0m'
            return f"{int(v // 60)}h {int(v % 60)}m" if int(v // 60) else f"{int(v % 60)}m"
        return str(int(v))

    for col, mi in enumerate(metrics_info):
        ax_card = fig.add_subplot(gs[0, col])
        ax_card.set_facecolor('#F4F5F7')
        ax_card.set_xlim(0, 1); ax_card.set_ylim(0, 1); ax_card.axis('off')

        ax_card.add_patch(mpatches.Rectangle((0,0), 1, 1, fill=True, color='#F4F5F7', ec='#E5E7EB', lw=1))
        ax_card.add_patch(mpatches.Rectangle((0, 0.94), 1, 0.06, fill=True, color='#0F6E56', lw=0))
        ax_card.text(0.5, 0.72, mi['title'], ha='center', va='center', color='#6B7280', fontsize=8, fontweight='bold')
        ax_card.text(0.5, 0.36, f"{fmt_val(totals[mi['key']], mi['key'])}", ha='center', va='center', color='#1F2937', fontsize=16, fontweight='bold')

        ax_list = fig.add_subplot(gs[1, col])
        ax_list.set_facecolor('#FFFFFF'); ax_list.set_xlim(0, 1); ax_list.set_ylim(0, 1); ax_list.axis('off')
        ax_list.text(0.0, 0.95, 'AGENT', ha='left', va='center', color='#6B7280', fontsize=7.5, fontweight='bold')
        ax_list.text(1.0, 0.95, mi['col_title'], ha='right', va='center', color='#6B7280', fontsize=7.5, fontweight='bold')

        sorted_teams = sorted(teams_data, key=lambda x: x[f"val_{mi['key']}"], reverse=True)
        y_pos = 0.80
        for team in sorted_teams[:7]:
            name_str  = f"{team['team_name']}:"
            ax_list.text(0.0, y_pos, name_str, ha='left', va='center', color='#185FA5', fontsize=8, fontweight='bold')
            ax_list.plot([0, min(len(name_str)*0.026, 0.6)], [y_pos - 0.04, y_pos - 0.04], color='#185FA5', lw=0.5)
            ax_list.text(1.0, y_pos, fmt_val(team[f"val_{mi['key']}"], mi['key']), ha='right', va='center', color='#1F2937', fontsize=9, fontweight='bold')
            y_pos -= 0.13

    plt.subplots_adjust(left=0.01, right=0.99, top=0.95, bottom=0.05)
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=180, bbox_inches='tight', transparent=True); plt.close(fig); buf.seek(0)
    return buf

def buf_donut(result_counts):
    items = [(k,v) for k,v in result_counts.most_common() if k not in ('Unknown','') and v > 0]
    if not items: items = [('No Data', 1)]
    labels, sizes = [k for k,v in items], [v for k,v in items]
    clrs = [DONUT_PALETTE[i % len(DONUT_PALETTE)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(4.5, 3.2))
    fig.patch.set_alpha(0); ax.set_facecolor('none')
    ax.pie(sizes, colors=clrs, startangle=90, wedgeprops=dict(width=0.45, edgecolor='white', linewidth=1.5))
    total = sum(sizes)
    ax.text(0, 0.08, str(total), ha='center', va='center', fontsize=22, fontweight='bold', color='#2C2C2A')
    ax.text(0, -0.22, 'total calls', ha='center', va='center', fontsize=9, color='#888780')
    legend = [mpatches.Patch(facecolor=clrs[i], label=f'{labels[i]} — {sizes[i]} ({sizes[i]/total*100:.0f}%)') for i in range(len(labels))]
    ax.legend(handles=legend, loc='center left', bbox_to_anchor=(0.95, 0.5), fontsize=8.5, frameon=False)
    ax.axis('equal'); fig.tight_layout(pad=0.5)
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=180, bbox_inches='tight', transparent=True); plt.close(fig); buf.seek(0)
    return buf

def buf_hbar(list_counts):
    items = [(k, v) for k, v in list_counts.most_common() if k and v > 0]
    is_empty = False
    if not items: items = [('No Data', 0)]; is_empty = True
    items = items[::-1]
    labels, vals = [(lbl[:28] + '...' if len(lbl) > 30 else lbl) for lbl, v in items], [v for lbl, v in items]
    clrs = ['#8A8984'] * len(vals)
    if len(vals) >= 2 and not is_empty: clrs[-2] = '#9C3B1C'
    fig, ax = plt.subplots(figsize=(4.2, max(1.2, len(labels)*0.45 + 0.5)))
    fig.patch.set_alpha(0); ax.set_facecolor('none')
    bars = ax.barh(labels, vals, color=clrs, height=0.3, edgecolor='none')
    max_val = max(vals) if max(vals) > 0 else 1
    for bar, val in zip(bars, vals):
        ax.text(bar.get_width() + max_val*0.02, bar.get_y() + bar.get_height()/2, '' if is_empty else str(val), va='center', fontsize=9.5, color='#4A4A48', fontweight='bold')
    ax.set_xlim(0, max_val * 1.2); ax.set_ylim(-0.5, len(labels) - 0.5)
    ax.tick_params(axis='y', labelsize=9, colors='#4A4A48', length=4, width=0.8)
    ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
    if is_empty:
        ax.tick_params(axis='x', which='both', bottom=False, labelbottom=False); ax.spines['bottom'].set_visible(False); ax.spines['left'].set_visible(False)
    else:
        ax.spines['left'].set_color('#D3D1C7'); ax.tick_params(axis='x', labelsize=8, colors='#888780', length=4); ax.spines['bottom'].set_color('#D3D1C7')
    fig.tight_layout(pad=0.5)
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=180, bbox_inches='tight', transparent=True); plt.close(fig); buf.seek(0)
    return buf

# ── Report Building Blocks ─────────────────────────────────────────────────
def metric_table(stats, styles):
    rc = stats['result_counts']
    def card(label, value, sub=''):
        lbl = label[:16] + '..' if len(label) > 18 else label
        return [Paragraph(lbl, ParagraphStyle('ml', fontName='Helvetica', fontSize=7.5, textColor=GRAY_MED)),
                Paragraph(str(value), ParagraphStyle('mv', fontName='Helvetica-Bold', fontSize=18, textColor=BLACK, leading=22)),
                Paragraph(sub, ParagraphStyle('ms', fontName='Helvetica', fontSize=7.5, textColor=GRAY_MED)) if sub else Spacer(1,1)]
    cards = [card('Dialing Time', stats['dial_str']), card('Total Calls', str(stats['total_calls'])), card('Appointments', str(stats['total_appts'])), card('Leads', str(stats['total_leads']))]
    for k, v in rc.most_common():
        if k.lower() in ('unknown', '') or v == 0: continue
        cards.append(card(k, str(v), f"{(v / stats['total_calls'] * 100):.1f}%" if stats['total_calls'] else '—'))
    data = []
    for i in range(0, len(cards), 4):
        row = cards[i:i+4]
        while len(row) < 4: row.append('')
        data.append(row)
    t = Table(data, colWidths=[1.77*inch]*4)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),GRAY_LIGHT),('INNERGRID',(0,0),(-1,-1),0.5,BORDER),('BOX',(0,0),(-1,-1),0.5,BORDER),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10)]))
    return t

def team_summary_table(teams_data, styles):
    hdr_st, cell_st, num_st = ParagraphStyle('ch', fontName='Helvetica-Bold', fontSize=8, textColor=WHITE), ParagraphStyle('cc', fontName='Helvetica', fontSize=8, textColor=GRAY_TEXT), ParagraphStyle('cn', fontName='Helvetica-Bold', fontSize=8, textColor=BLACK)
    headers = ['Agent', 'Dial Time', 'Talk Time', 'Calls', 'Drop Voicemail', 'No Answer', 'Other', 'Appts', 'Leads']
    rows = [[Paragraph(h, hdr_st) for h in headers]]
    for t in teams_data:
        rc = t['stats']['result_counts']
        vmail = sum(v for k,v in rc.items() if 'voicemail' in k.lower())
        no_ans = sum(v for k,v in rc.items() if 'no answer' in k.lower() or 'machine no answer' in k.lower())
        oth = max(0, t['stats']['total_calls'] - (vmail + no_ans))
        rows.append([Paragraph(t['team_name'], cell_st), Paragraph(t['stats']['dial_str'], cell_st), Paragraph(t['stats'].get('talk_str', '0m'), cell_st), Paragraph(str(t['stats']['total_calls']), num_st), Paragraph(str(vmail), cell_st), Paragraph(str(no_ans), cell_st), Paragraph(str(oth), cell_st), Paragraph(str(t['stats']['total_appts']), num_st), Paragraph(str(t['stats'].get('total_leads', 0)), cell_st)])
    t = Table(rows, colWidths=[1.2*inch, 0.65*inch, 0.65*inch, 0.55*inch, 0.9*inch, 0.8*inch, 0.55*inch, 0.55*inch, 0.55*inch], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),BLUE),('ROWBACKGROUNDS',(0,1),(-1,-1),[WHITE,GRAY_LIGHT]),('GRID',(0,0),(-1,-1),0.3,BORDER),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    return t

# ── Builder functions ──────────────────────────────────────────────────────
def build_pdf_report(teams_data):
    styles = build_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.65*inch, rightMargin=0.65*inch, topMargin=0.65*inch, bottomMargin=0.65*inch)
    story = []

    all_dates = []
    for t in teams_data: all_dates += list(t['stats']['date_counts'].keys())
    if all_dates:
        all_dates = sorted(set(all_dates), key=lambda d: datetime.strptime(d,'%m/%d/%Y'))
        period = f"{datetime.strptime(all_dates[0], '%m/%d/%Y').strftime('%B %d')} – {datetime.strptime(all_dates[-1],'%m/%d/%Y').strftime('%B %d, %Y')}"
    else:
        period = datetime.now().strftime('%B %Y')

    # Main Dashboard Page
    story.append(Paragraph('Weekly Mojo Performance Report', styles['title']))
    story.append(Paragraph(f'Team overview &nbsp;·&nbsp; {period}', styles['subtitle']))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width='100%', thickness=0.5, color=BORDER, spaceAfter=10))
    story.append(Paragraph('Team summary', styles['section']))
    story.append(team_summary_table(teams_data, styles))
    story.append(Spacer(1, 16))
    story.append(Paragraph('Leaderboard', styles['section']))
    story.append(Image(buf_leaderboard(teams_data), width=7.2*inch, height=3.12*inch))

    # Per Team Breakdowns
    for t in teams_data:
        story.append(PageBreak())
        block = []
        block.append(Paragraph('Weekly Mojo Performance Report', styles['subtitle']))
        block.append(Paragraph(f"{t['team_name']}", styles['team_h']))
        block.append(Paragraph(f"Caller ID: {t['caller_id']} &nbsp;·&nbsp; Source: {t['filename']}", styles['subtitle']))
        block.append(Spacer(1, 6))
        block.append(HRFlowable(width='100%', thickness=0.5, color=BORDER, spaceAfter=8))

        appts = t['stats']['total_appts']
        btxt, b_bg, b_txt, b_bdr = (f'<b>{appts} appointment(s) booked</b>', GREEN_BG, GREEN_TEXT, colors.HexColor('#85C785')) if appts > 0 else ('<b>0 appointments booked</b>', RED_BG, RED_TEXT, colors.HexColor('#F09595'))
        banner = Table([[Paragraph(btxt, ParagraphStyle('b', fontName='Helvetica', fontSize=9, textColor=b_txt))]], colWidths=[7.0*inch])
        banner.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),b_bg),('BOX',(0,0),(-1,-1),0.5,b_bdr),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),12)]))
        block += [banner, Spacer(1, 10), Paragraph('Summary metrics', styles['section']), metric_table(t['stats'], styles), Spacer(1, 12), Paragraph('Call outcome breakdown', styles['section'])]

        donut_img = Image(buf_donut(t['stats']['result_counts']), width=3.4*inch, height=2.4*inch)
        hbar_img  = Image(buf_hbar(t['stats']['list_counts']), width=3.9*inch, height=max(1.2, len(t['stats']['list_counts'])*0.45 + 0.5)*inch)
        charts = Table([[Paragraph('By result type', styles['small']), Paragraph('By campaign list', styles['small'])], [donut_img, hbar_img]], colWidths=[3.4*inch, 3.9*inch])
        charts.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('RIGHTPADDING',(0,0),(-1,-1),8)]))
        block.append(charts)
        story.append(KeepTogether(block))

    doc.build(story)
    buf.seek(0)
    return buf

# ── Streamlit UI Application ───────────────────────────────────────────────
st.set_page_config(page_title="Mojo Report Generator", page_icon="📊", layout="centered")
st.title("📊 Weekly Mojo Performance Report Generator")
st.write("Upload your Mojo session/agent report PDFs to generate a master breakdown instantly.")

uploaded_files = st.file_uploader("Choose Mojo Report PDFs", type="pdf", accept_multiple_files=True)

if uploaded_files:
    st.success(f"Successfully loaded {len(uploaded_files)} files!")
    teams_data = []
    
    for f in uploaded_files:
        bytes_data = f.read()
        stats = extract_session_records(bytes_data)
        
        base = os.path.splitext(f.name)[0]
        base = re.sub(r'[_\-](report|session|may|calls?|data|log|2026.*)$', '', base, flags=re.IGNORECASE)
        t_name = re.sub(r'\s+Parks?$', ' Park', base.replace('_',' ').replace('-',' ').strip().title(), flags=re.IGNORECASE) or 'Unknown'
        
        teams_data.append({
            'filename': f.name,
            'caller_id': stats['agent_name'],
            'team_name': t_name,
            'stats': stats,
        })
        st.text(f"Processed → {t_name} ({stats['total_calls']} calls)")

    if st.button("🚀 Generate Master PDF Report", type="primary"):
        with st.spinner("Compiling database and rendering charts..."):
            pdf_out = build_pdf_report(teams_data)
            st.balloons()
            st.download_button(
                label="📥 Download Master Report PDF",
                data=pdf_out,
                file_name="Weekly_Mojo_Performance_Report.pdf",
                mime="application/pdf"
            )
