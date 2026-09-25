#!/usr/bin/env python3
"""
Convert markdown ruleset to PDF using Headless Chrome with KaTeX and Mermaid support.
"""

import os
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path

HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  
  <!-- KaTeX CSS -->
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">
  
  <!-- Marked, KaTeX, and Mermaid JS -->
  <script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10.6.1/dist/mermaid.min.js"></script>

  <style>
    @page {{
      size: A4;
      margin: 20mm 15mm 20mm 15mm;
      @bottom-right {{
        content: counter(page);
      }}
    }}
    
    *, *:before, *:after {{
      box-sizing: border-box;
    }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Micro Hei", "Droid Sans Fallback", sans-serif;
      font-size: 13px;
      line-height: 1.65;
      color: #1f2328;
      background-color: #ffffff;
      padding: 0;
      margin: 0;
      -webkit-font-smoothing: antialiased;
    }}

    h1 {{
      font-size: 22px;
      font-weight: 700;
      color: #0d1117;
      border-bottom: 2px solid #0969da;
      padding-bottom: 8px;
      margin-top: 0;
      margin-bottom: 16px;
      page-break-after: avoid;
    }}

    h2 {{
      font-size: 16px;
      font-weight: 600;
      color: #1f2328;
      border-bottom: 1px solid #d0d7de;
      padding-bottom: 6px;
      margin-top: 24px;
      margin-bottom: 12px;
      page-break-after: avoid;
    }}

    h3 {{
      font-size: 14px;
      font-weight: 600;
      color: #24292f;
      margin-top: 18px;
      margin-bottom: 8px;
      page-break-after: avoid;
    }}

    h4 {{
      font-size: 13px;
      font-weight: 600;
      color: #24292f;
      margin-top: 14px;
      margin-bottom: 6px;
      page-break-after: avoid;
    }}

    p {{
      margin-top: 0;
      margin-bottom: 10px;
      word-break: break-word;
    }}

    ul, ol {{
      margin-top: 0;
      margin-bottom: 10px;
      padding-left: 22px;
    }}

    li {{
      margin-bottom: 4px;
    }}

    li > ul, li > ol {{
      margin-top: 4px;
      margin-bottom: 4px;
    }}

    strong {{
      font-weight: 600;
      color: #0d1117;
    }}

    blockquote {{
      margin: 12px 0;
      padding: 10px 16px;
      color: #57606a;
      background-color: #f6f8fa;
      border-left: 4px solid #0969da;
      border-radius: 0 4px 4px 0;
      page-break-inside: avoid;
    }}

    blockquote p:last-child {{
      margin-bottom: 0;
    }}

    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 14px 0;
      font-size: 12px;
      line-height: 1.5;
      page-break-inside: avoid;
    }}

    th, td {{
      border: 1px solid #d0d7de;
      padding: 7px 10px;
      text-align: left;
      vertical-align: top;
    }}

    th {{
      background-color: #f6f8fa;
      font-weight: 600;
      color: #24292f;
    }}

    tr:nth-child(even) {{
      background-color: #fcfcfc;
    }}

    code {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace, "WenQuanYi Micro Hei Mono";
      font-size: 11.5px;
      background-color: #eff1f3;
      padding: 2px 5px;
      border-radius: 4px;
      border: 1px solid #e1e4e8;
      color: #cf222e;
    }}

    pre {{
      background-color: #f6f8fa;
      padding: 12px 14px;
      border-radius: 6px;
      border: 1px solid #d0d7de;
      overflow-x: auto;
      page-break-inside: avoid;
      margin: 12px 0;
    }}

    pre code {{
      background: none;
      padding: 0;
      border: none;
      color: #24292f;
      font-size: 11px;
      line-height: 1.45;
    }}

    .mermaid {{
      text-align: center;
      margin: 14px auto;
      background: #fafbfc;
      border: 1px solid #e1e4e8;
      border-radius: 6px;
      padding: 10px;
      page-break-inside: avoid;
    }}

    .mermaid svg {{
      max-width: 95% !important;
      max-height: 195mm !important;
      width: auto !important;
      height: auto !important;
      display: inline-block;
    }}

    hr {{
      height: 1px;
      background-color: #d0d7de;
      border: none;
      margin: 18px 0;
    }}

    a {{
      color: #0969da;
      text-decoration: none;
    }}

    .katex-display {{
      margin: 10px 0;
      page-break-inside: avoid;
      overflow-x: auto;
      overflow-y: hidden;
    }}

    .status-badge {{
      display: inline-block;
      padding: 2px 6px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 10px;
      background: #ddf4ff;
      color: #0969da;
    }}

    .footer {{
      margin-top: 30px;
      padding-top: 10px;
      border-top: 1px solid #e1e4e8;
      font-size: 11px;
      color: #8c959f;
      text-align: right;
    }}
  </style>
</head>
<body>
  <div id="content"></div>

  <script id="raw-markdown" type="text/plain">{markdown_json}</script>

  <script>
    mermaid.initialize({{
      startOnLoad: false,
      theme: 'neutral',
      fontFamily: '"WenQuanYi Micro Hei", "Droid Sans Fallback", sans-serif',
      securityLevel: 'loose',
      flowchart: {{
        nodeSpacing: 25,
        rankSpacing: 30,
        curve: 'basis'
      }}
    }});

    const raw = JSON.parse(document.getElementById('raw-markdown').textContent);

    // Step 1: Protect LaTeX math before Markdown parser sees it
    const mathTokens = [];

    // Display math $$...$$
    let text = raw.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, (match, formula) => {{
      const token = 'KATEXDISPLAYTOKEN' + mathTokens.length + 'XYZ';
      mathTokens.push({{ type: 'display', formula: formula }});
      return token;
    }});

    // Inline math $...$ (avoiding double $$ and escaped \\$)
    text = text.replace(/(?<![\\\\$])\\$([^$\\n]+?)\\$/g, (match, formula) => {{
      const token = 'KATEXINLINETOKEN' + mathTokens.length + 'XYZ';
      mathTokens.push({{ type: 'inline', formula: formula }});
      return token;
    }});

    // Step 2: Configure marked
    const renderer = new marked.Renderer();
    const origCode = renderer.code.bind(renderer);
    renderer.code = function(code, lang, escaped) {{
      if (lang === 'mermaid') {{
        return '<div class="mermaid">' + code + '</div>';
      }}
      return origCode(code, lang, escaped);
    }};

    marked.setOptions({{
      renderer: renderer,
      gfm: true,
      breaks: false
    }});

    let html = marked.parse(text);

    // Step 3: Restore Math Tokens
    for (let i = 0; i < mathTokens.length; i++) {{
      const item = mathTokens[i];
      if (item.type === 'display') {{
        html = html.replace('KATEXDISPLAYTOKEN' + i + 'XYZ', '$$' + item.formula + '$$');
      }} else {{
        html = html.replace('KATEXINLINETOKEN' + i + 'XYZ', '$' + item.formula + '$');
      }}
    }}

    document.getElementById('content').innerHTML = html;

    // Step 4: Render KaTeX
    renderMathInElement(document.getElementById('content'), {{
      delimiters: [
        {{left: '$$', right: '$$', display: true}},
        {{left: '$', right: '$', display: false}}
      ],
      throwOnError: false
    }});

    // Step 5: Render Mermaid diagrams
    async function renderAll() {{
      try {{
        await mermaid.run({{
          querySelector: '.mermaid'
        }});
      }} catch (e) {{
        console.error('Mermaid render error:', e);
      }} finally {{
        document.body.setAttribute('data-ready', 'true');
      }}
    }}
    
    renderAll();
  </script>
</body>
</html>
"""

def find_chrome():
    candidates = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']
    for c in candidates:
        try:
            res = subprocess.run(['which', c], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    return None

def convert_md_to_pdf(md_path: Path, output_pdf_path: Path, keep_html=False):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("No chrome/chromium binary found on system.")

    md_text = md_path.read_text(encoding='utf-8')
    title = md_path.stem
    # Try finding first H1
    for line in md_text.splitlines():
        if line.startswith('# '):
            title = line[2:].strip()
            break

    html_content = HTML_SHELL.format(
        title=title,
        markdown_json=json.dumps(md_text)
    )

    temp_html = md_path.parent / f".tmp_{md_path.stem}.html"
    temp_html.write_text(html_content, encoding='utf-8')

    try:
        # Chrome print to pdf
        cmd = [
            chrome,
            '--headless=new',
            '--no-sandbox',
            '--disable-gpu',
            '--no-pdf-header-footer',
            '--run-all-compositor-stages-before-draw',
            '--virtual-time-budget=10000',
            f'--print-to-pdf={output_pdf_path.resolve()}',
            f'file://{temp_html.resolve()}'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            print(f"Error converting {md_path}:", res.stderr, file=sys.stderr)
            return False
        
        print(f"Successfully generated: {output_pdf_path} ({os.path.getsize(output_pdf_path):,} bytes)")
        return True
    finally:
        if not keep_html and temp_html.exists():
            temp_html.unlink()

def main():
    parser = argparse.ArgumentParser(description="Convert ruleset markdown to PDF")
    parser.add_argument("input_files", nargs="*", help="Markdown files to convert")
    parser.add_argument("--all", action="store_true", help="Convert all rules_cn.md found in docs/ruleset")
    args = parser.parse_args()

    files_to_convert = []
    if args.input_files:
        for f in args.input_files:
            p = Path(f)
            if p.exists():
                files_to_convert.append(p)
            else:
                print(f"Warning: File not found: {f}", file=sys.stderr)
    elif args.all or not args.input_files:
        # Default to finding rules_cn.md
        rules_files = sorted(Path("docs/ruleset").glob("**/rules_cn.md"))
        files_to_convert.extend(rules_files)

    if not files_to_convert:
        print("No files found to convert.")
        sys.exit(1)

    success_count = 0
    for md_file in files_to_convert:
        pdf_out = md_file.parent / f"{md_file.stem}.pdf"
        print(f"Converting {md_file} -> {pdf_out}...")
        if convert_md_to_pdf(md_file, pdf_out):
            success_count += 1

    print(f"\nDone! Converted {success_count}/{len(files_to_convert)} files.")

if __name__ == '__main__':
    main()
