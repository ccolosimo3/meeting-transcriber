/* Summary presentation only. Saved Markdown is never interpreted as HTML. */
(() => {
  "use strict";

  const MAX_BYTES = 128 * 1024;
  const MAX_TABLE_CELLS = 4096;
  const MAX_TOKENS = 10000;
  const MAX_LEVEL = 63;
  const tags = Object.freeze({
    paragraph_open: "p", blockquote_open: "blockquote",
    bullet_list_open: "ul", ordered_list_open: "ol", list_item_open: "li",
    em_open: "em", strong_open: "strong", s_open: "del",
    table_open: "table", thead_open: "thead", tbody_open: "tbody",
    tr_open: "tr", th_open: "th", td_open: "td"
  });
  let parser;

  function admitted(source) {
    if (source.length > MAX_BYTES || new TextEncoder().encode(source).length > MAX_BYTES) return false;
    // Bound table padding before parse() allocates cells. Count raw physical
    // lines/pipes conservatively, including escaped pipes and fenced content.
    let lines = 1, pipes = 0, maximum = 0;
    for (const char of source.replace(/\r\n?/g, "\n")) {
      if (char === "|") pipes++;
      if (char === "\n") {
        lines++;
        maximum = Math.max(maximum, pipes);
        pipes = 0;
      }
    }
    maximum = Math.max(maximum, pipes);
    return !maximum || lines * (maximum + 1) <= MAX_TABLE_CELLS;
  }

  function tokensFor(source) {
    if (!parser) {
      parser = window.markdownit({ html: false, linkify: false, typographer: false, maxNesting: 64 })
        .disable(["link", "image", "autolink", "reference", "html_block", "html_inline"]);
    }
    const tokens = parser.parse(source, {});
    if (!Array.isArray(tokens)) throw new Error("Invalid tokens");
    const frames = [{ tokens, index: 0 }], flat = [];
    let count = 0;
    while (frames.length) {
      const frame = frames[frames.length - 1];
      if (frame.index === frame.tokens.length) { frames.pop(); continue; }
      const token = frame.tokens[frame.index++];
      if (++count > MAX_TOKENS || !token || !Number.isInteger(token.level) || token.level < 0 || token.level >= MAX_LEVEL) {
        throw new Error("Formatting limit");
      }
      if (token.type === "inline") {
        if (!Array.isArray(token.children)) throw new Error("Invalid inline tokens");
        frames.push({ tokens: token.children, index: 0 });
      } else {
        if (token.children?.length) throw new Error("Unexpected children");
        flat.push(token);
      }
    }
    return flat;
  }

  function fragmentFor(document, tokens) {
    const fragment = document.createDocumentFragment();
    const stack = [{ node: fragment, close: null }];
    const current = () => stack[stack.length - 1].node;
    const text = value => {
      if (typeof value !== "string") throw new Error("Invalid text");
      return document.createTextNode(value);
    };
    for (const token of tokens) {
      const type = token.type;
      if (Object.hasOwn(tags, type) || type === "heading_open") {
        const tag = type === "heading_open" ? token.tag : tags[type];
        if (token.nesting !== 1 || (type === "heading_open" && !/^h[1-6]$/.test(tag))) throw new Error("Invalid opening token");
        const parent = current();
        const hidden = type === "paragraph_open" && token.hidden;
        const node = hidden ? parent : document.createElement(tag);
        if (!hidden) {
          if (type === "ordered_list_open") {
            const start = token.attrs?.find(attribute => attribute[0] === "start")?.[1];
            if (start !== undefined) {
              if (!/^\d{1,9}$/.test(String(start))) throw new Error("Invalid list start");
              node.setAttribute("start", String(Number(start)));
            }
          }
          if (type === "th_open") node.setAttribute("scope", "col");
          if (type === "table_open") {
            const wrapper = document.createElement("div");
            wrapper.className = "summary-table-scroll";
            wrapper.append(node);
            parent.append(wrapper);
          } else parent.append(node);
        }
        stack.push({ node, close: type.replace(/_open$/, "_close") });
      } else if (typeof type === "string" && type.endsWith("_close")) {
        if (token.nesting !== -1 || stack.length === 1 || stack.pop().close !== type) throw new Error("Invalid closing token");
      } else if (token.nesting !== 0) {
        throw new Error("Invalid leaf token");
      } else if (type === "text") {
        current().append(text(token.content));
      } else if (type === "softbreak") {
        current().append(text("\n"));
      } else if (type === "hardbreak" || type === "hr") {
        current().append(document.createElement(type === "hardbreak" ? "br" : "hr"));
      } else if (type === "code_inline" || type === "code_block" || type === "fence") {
        const code = document.createElement("code");
        code.append(text(token.content));
        if (type === "code_inline") current().append(code);
        else {
          const pre = document.createElement("pre");
          pre.append(code);
          current().append(pre);
        }
      } else throw new Error("Unsupported token");
    }
    if (stack.length !== 1) throw new Error("Unclosed tokens");
    return fragment;
  }

  function render(target, source) {
    try {
      if (typeof source !== "string" || !admitted(source)) return false;
      const fragment = fragmentFor(target.ownerDocument, tokensFor(source));
      target.replaceChildren(fragment);
      return true;
    } catch (_) {
      // Caller retains the complete original source and its existing warnings.
      return false;
    }
  }

  window.SummaryMarkdown = Object.freeze({ render });
})();
