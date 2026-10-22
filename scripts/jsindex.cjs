#!/usr/bin/env node
/* Syntax-only JS/TS indexing. Loads the compiler, never the target program. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');

function loadCompiler(input) {
  if (input.typescript) return require(path.resolve(input.typescript));
  for (const root of [input.root, __dirname]) {
    try { return require(createRequire(path.join(root, 'package.json')).resolve('typescript')); }
    catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('TypeScript compiler unavailable; pass --typescript <module directory or typescript.js>');
}

function resolver(ts, input) {
  const root = path.resolve(input.root), selected = new Set(input.files), configs = new Map();
  const inside = file => { const rel = path.relative(root, path.resolve(file)); return rel !== '..' && !rel.startsWith('..' + path.sep) && !path.isAbsolute(rel); };
  function configuration(relative) {
    let directory = path.dirname(path.join(root, relative)), config;
    if (input.tsconfig) config = path.resolve(input.tsconfig);
    else {
      while (inside(directory)) {
        config = ['tsconfig.json', 'jsconfig.json'].map(name => path.join(directory, name)).find(file => fs.existsSync(file));
        if (config || directory === root) break;
        directory = path.dirname(directory);
      }
    }
    if (!config) return {options: {allowJs: true, moduleResolution: ts.ModuleResolutionKind.Node10}, files: [], errors: []};
    if (!inside(config)) throw new Error('tsconfig must be inside the project');
    if (configs.has(config)) return configs.get(config);
    const files = new Map(), errors = [];
    const readFile = file => {
      if (!inside(file)) { errors.push('config dependency outside project: ' + file); return undefined; }
      try { const raw = fs.readFileSync(file); files.set(path.relative(root, file).split(path.sep).join('/'), crypto.createHash('sha256').update(raw).digest('hex')); return raw.toString('utf8'); }
      catch { return undefined; }
    };
    const read = ts.readConfigFile(config, readFile);
    const parsed = ts.parseJsonConfigFileContent(read.config || {}, {...ts.sys, readFile, readDirectory: () => []}, path.dirname(config), {allowJs: true}, config);
    for (const diagnostic of [read.error, ...parsed.errors].filter(Boolean)) {
      // An empty project is valid for resolution-only use.
      if (diagnostic.code !== 18003) errors.push(ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'));
    }
    const result = {options: parsed.options, files: [...files].map(([path, content_hash]) => ({path, content_hash})), errors};
    configs.set(config, result);
    return result;
  }
  return (relative, imports) => {
    const config = configuration(relative), resolved = [], unresolved = [];
    for (const specifier of imports) {
      const result = ts.resolveModuleName(specifier, path.join(root, relative), config.options, ts.sys).resolvedModule;
      const target = result && path.relative(root, result.resolvedFileName).split(path.sep).join('/');
      if (!config.errors.length && target && inside(result.resolvedFileName) && selected.has(target)) resolved.push({import: specifier, path: target, kind: 'typescript-resolver'});
      else unresolved.push({import: specifier, reason: config.errors.length ? 'invalid resolution config' : 'external, unresolved or outside selected sources'});
    }
    return {resolved_imports: resolved, unresolved_imports: unresolved, resolution_configs: config.files,
            diagnostics: config.errors.map(reason => ({path: relative, reason: 'module resolution: ' + reason}))};
  };
}

function analyze(ts, root, relative, resolveImports) {
  const raw = fs.readFileSync(path.join(root, relative));
  const text = new TextDecoder('utf-8', {fatal: true}).decode(raw);
  const source = ts.createSourceFile(relative, text, ts.ScriptTarget.Latest, true);
  if (source.parseDiagnostics.length) {
    return {diagnostics: source.parseDiagnostics.map(d => ({path: relative,
      line: source.getLineAndCharacterOfPosition(d.start || 0).line + 1,
      reason: ts.flattenDiagnosticMessageText(d.messageText, '\n')}))};
  }
  const printer = ts.createPrinter({removeComments: true, newLine: ts.NewLineKind.LineFeed});
  const hash = value => crypto.createHash('sha256').update(value).digest('hex');
  const printed = node => printer.printNode(ts.EmitHint.Unspecified, node, source);
  const canonical = node => {
    const children = node.getChildren(source).filter(child => !(child.kind >= ts.SyntaxKind.FirstJSDocNode && child.kind <= ts.SyntaxKind.LastJSDocNode));
    return children.length ? children.map(canonical).join('|') : `${node.kind}:${node.getText(source)}`;
  };
  const headerCanonical = node => {
    const children = node.getChildren(source).filter(child => child !== node.body && !(child.kind >= ts.SyntaxKind.FirstJSDocNode && child.kind <= ts.SyntaxKind.LastJSDocNode));
    return children.length ? children.map(headerCanonical).join('|') : `${node.kind}:${node.getText(source)}`;
  };
  const line = position => source.getLineAndCharacterOfPosition(position).line + 1;
  const imports = new Set(), importDetails = [], exports = [], exportedLocals = new Set(), dynamicImports = [];
  const symbols = [];
  const has = (node, kind) => !!node.modifiers?.some(modifier => modifier.kind === kind);
  const addImport = (node, target, kind) => {
    if (!ts.isStringLiteralLike(target)) return;
    imports.add(target.text);
    importDetails.push({path: target.text, line: line(node.getStart(source)), kind});
  };
  const exportNames = (statement, names) => {
    if (!has(statement, ts.SyntaxKind.ExportKeyword)) return;
    const isDefault = has(statement, ts.SyntaxKind.DefaultKeyword);
    for (const name of names) {
      exportedLocals.add(name);
      exports.push({name: isDefault ? 'default' : name, local: name, line: line(statement.getStart(source)), kind: 'declaration'});
    }
  };
  for (const statement of source.statements) {
    if (ts.isImportDeclaration(statement)) addImport(statement, statement.moduleSpecifier, 'import');
    if (ts.isImportEqualsDeclaration(statement) && ts.isExternalModuleReference(statement.moduleReference)
        && statement.moduleReference.expression) addImport(statement, statement.moduleReference.expression, 'require');
    if (ts.isExportDeclaration(statement)) {
      const target = statement.moduleSpecifier;
      if (target) addImport(statement, target, 'reexport');
      if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          const local = (element.propertyName || element.name).text;
          exports.push({name: element.name.text, local, source: target?.text,
                        line: line(element.getStart(source)), kind: target ? 'reexport' : 'named'});
          if (!target) exportedLocals.add(local);
        }
      } else {
        exports.push({name: statement.exportClause?.name?.text || '*', source: target?.text,
                      line: line(statement.getStart(source)), kind: 'reexport'});
      }
    }
    if (ts.isExportAssignment(statement)) {
      const local = ts.isIdentifier(statement.expression) ? statement.expression.text : '<default>';
      exports.push({name: statement.isExportEquals ? 'export=' : 'default', local,
                    line: line(statement.getStart(source)), kind: 'assignment'});
      exportedLocals.add(local);
    }
    if (ts.isVariableStatement(statement)) {
      exportNames(statement, statement.declarationList.declarations.filter(d => ts.isIdentifier(d.name)).map(d => d.name.text));
    } else if (statement.name && ts.isIdentifier(statement.name)) {
      exportNames(statement, [statement.name.text]);
    } else if (has(statement, ts.SyntaxKind.DefaultKeyword)) exportNames(statement, ['<default>']);
  }
  // Literal dynamic imports are dependencies, not proof that a branch executes.
  const walkImports = node => {
    if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword
        || (ts.isIdentifier(node.expression) && node.expression.text === 'require')) && node.arguments[0]) {
      if (ts.isStringLiteralLike(node.arguments[0])) addImport(node, node.arguments[0], 'dynamic-literal');
      else dynamicImports.push({line: line(node.getStart(source)), reason: 'non-literal dynamic import/require; target unknown'});
    }
    ts.forEachChild(node, walkImports);
  };
  walkImports(source);

  function add(node, name, kind, topName, body, callable = node) {
    const start = node.getStart(source), end = node.getEnd();
    const visible = !has(node, ts.SyntaxKind.PrivateKeyword) && !has(node, ts.SyntaxKind.ProtectedKeyword)
      && !(node.name && ts.isPrivateIdentifier(node.name));
    const symbol = {name, kind, exported: !!topName && exportedLocals.has(topName) && visible, visible, export_local: topName,
      line: line(start), end_line: line(Math.max(start, end - 1)), line_quality: 'ast',
      signature: text.slice(start, body ? body.getStart(source) : end).trim(),
      content_hash: hash(canonical(node)), doc: (node.jsDoc || []).map(doc => doc.comment || '').filter(x => typeof x === 'string').join('\n')};
    if (callable.parameters) symbol.params = callable.parameters.map(p => printed(p)).join(', ');
    if (callable.type) symbol.results = printed(callable.type);
    symbol.signature_hash = hash(headerCanonical(node));
    if (body) symbol.body_hash = hash(canonical(body));
    symbols.push(symbol);
    if (body && ts.isBlock(body)) statements(body.statements, name + '.');
    else if (body && ts.isObjectLiteralExpression(body)) objectMembers(body, name + '.<return>', null);
  }
  function objectMembers(object, parent, topName) {
    for (const member of object.properties) {
      if (!member.name) continue;
      const name = parent + '.' + member.name.getText(source).replace(/^['"]|['"]$/g, '');
      if (ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
        add(member, name, 'method', topName, member.body);
      } else if (ts.isPropertyAssignment(member) && (ts.isArrowFunction(member.initializer) || ts.isFunctionExpression(member.initializer))) {
        add(member, name, 'method', topName, member.initializer.body, member.initializer);
      } else if (ts.isPropertyAssignment(member) && ts.isObjectLiteralExpression(member.initializer)) {
        objectMembers(member.initializer, name, topName);
      }
    }
  }
  function statements(nodes, parent = '', namespaceRoot = null) {
    for (const node of nodes) {
      const local = node.name?.getText(source) || '<default>';
      const name = parent + local;
      const exportedRoot = parent ? (namespaceRoot && has(node, ts.SyntaxKind.ExportKeyword) ? namespaceRoot : null) : local;
      if (ts.isFunctionDeclaration(node)) add(node, name, 'func', exportedRoot, node.body);
      else if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) {
        add(node, name, ts.isClassDeclaration(node) ? 'class' : 'interface', exportedRoot);
        for (const member of node.members) {
          const memberName = member.name?.getText(source) || (ts.isConstructorDeclaration(member) ? 'constructor' : null);
          if (!memberName) continue;
          if (ts.isMethodDeclaration(member) || ts.isMethodSignature(member) || ts.isConstructorDeclaration(member)
              || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
            add(member, name + '.' + memberName, 'method', exportedRoot, member.body);
          } else if (ts.isPropertyDeclaration(member) && member.initializer
                     && (ts.isArrowFunction(member.initializer) || ts.isFunctionExpression(member.initializer))) {
            add(member, name + '.' + memberName, 'method', exportedRoot, member.initializer.body, member.initializer);
          }
        }
      } else if (ts.isTypeAliasDeclaration(node) || ts.isEnumDeclaration(node)) {
        add(node, name, ts.isEnumDeclaration(node) ? 'enum' : 'type', exportedRoot);
      } else if (ts.isVariableStatement(node)) {
        for (const declaration of node.declarationList.declarations) {
          if (!ts.isIdentifier(declaration.name)) continue;
          const localName = declaration.name.text, value = declaration.initializer;
          const isFunction = value && (ts.isArrowFunction(value) || ts.isFunctionExpression(value));
          add(declaration, parent + localName, isFunction ? 'func' :
              (node.declarationList.flags & ts.NodeFlags.Const ? 'const' : 'var'), parent ? exportedRoot : localName, isFunction ? value.body : null, isFunction ? value : declaration);
          if (value && ts.isObjectLiteralExpression(value)) objectMembers(value, parent + localName, parent ? exportedRoot : localName);
        }
      } else if (ts.isExportAssignment(node)) {
        if (ts.isArrowFunction(node.expression) || ts.isFunctionExpression(node.expression)) add(node.expression, '<default>', 'func', '<default>', node.expression.body);
        else if (ts.isObjectLiteralExpression(node.expression)) objectMembers(node.expression, '<default>', '<default>');
      } else if (ts.isModuleDeclaration(node)) {
        add(node, name, 'namespace', exportedRoot);
        if (node.body) statements(ts.isModuleBlock(node.body) ? node.body.statements : [node.body], name + '.', exportedRoot);
      } else if (ts.isReturnStatement(node) && node.expression && ts.isObjectLiteralExpression(node.expression)) {
        objectMembers(node.expression, parent + '<return>', null);
      } else {
        // Conditional/local declarations retain lexical scope; callback bodies are not public exports.
        const visit = child => {
          if (ts.isArrowFunction(child) || ts.isFunctionExpression(child)) return;
          if (ts.isBlock(child)) statements(child.statements, parent);
          else if (ts.isStatement(child)) statements([child], parent);
          else ts.forEachChild(child, visit);
        };
        ts.forEachChild(node, visit);
      }
    }
  }
  statements(source.statements);
  // CommonJS exports are explicit metadata; dynamic object construction is not resolved.
  for (const node of source.statements) {
    if (!ts.isExpressionStatement(node) || !ts.isBinaryExpression(node.expression)
        || node.expression.operatorToken.kind !== ts.SyntaxKind.EqualsToken) continue;
    const left = node.expression.left.getText(source), right = node.expression.right;
    if (left === 'module.exports' || /^(?:module\.)?exports\./.test(left)) {
      if (ts.isIdentifier(right)) exportedLocals.add(right.text);
      if (ts.isArrowFunction(right) || ts.isFunctionExpression(right)) {
        exportedLocals.add(left);
        add(right, left, 'func', left, right.body);
      }
      if (ts.isObjectLiteralExpression(right)) {
        exportedLocals.add(left);
        objectMembers(right, left, left);
        for (const property of right.properties) {
          const local = ts.isShorthandPropertyAssignment(property) ? property.name.text :
            (property.initializer && ts.isIdentifier(property.initializer) ? property.initializer.text : null);
          if (local) exportedLocals.add(local);
          exports.push({name: property.name?.getText(source), local, line: line(property.getStart(source)), kind: 'commonjs'});
        }
      } else exports.push({name: left, local: ts.isIdentifier(right) ? right.text : undefined,
                           line: line(node.getStart(source)), kind: 'commonjs'});
    }
  }
  for (const symbol of symbols) {
    if (symbol.export_local && exportedLocals.has(symbol.export_local) && symbol.visible) symbol.exported = true;
    delete symbol.visible;
    delete symbol.export_local;
  }
  const resolution = resolveImports(relative, [...imports]);
  const jsxRanges = [], exclusions = [];
  function comments(ranges) {
    for (const range of ranges || []) exclusions.push({start: range.pos, end: range.end, literal: false});
  }
  function collectExclusions(node) {
    comments(ts.getLeadingCommentRanges(text, node.pos));
    comments(ts.getTrailingCommentRanges(text, node.end));
    const jsx = ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node) || ts.isJsxFragment(node);
    if (jsx || ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
        || ts.isTemplateExpression(node) || node.kind === ts.SyntaxKind.RegularExpressionLiteral) {
      const range = {start: node.getStart(source), end: node.getEnd()};
      if (jsx) jsxRanges.push(range);
      exclusions.push({...range, literal: true});
      return; // Conservatively ignore interpolation/handlers within a literal/JSX.
    }
    ts.forEachChild(node, collectExclusions);
  }
  collectExclusions(source);
  // Comment trivia is shared by adjacent nodes; normalize duplicate/contained ranges.
  const candidateExclusions = [];
  for (const span of exclusions.sort((a, b) => a.start - b.start || b.end - a.end)) {
    const last = candidateExclusions[candidateExclusions.length - 1];
    if (last && span.start < last.end) {
      last.end = Math.max(last.end, span.end);
      last.literal ||= span.literal;
    } else candidateExclusions.push({...span});
  }
  return {file: {path: relative, language: /\.(?:ts|tsx|mts|cts)$/.test(relative) ? 'typescript' : 'javascript',
    jsx_ranges: jsxRanges, candidate_exclusions: candidateExclusions,
    candidate_syntax_valid: source.parseDiagnostics.length === 0,
    package: path.posix.dirname(relative) === '.' ? '' : path.posix.dirname(relative),
    imports: [...imports].sort(), import_details: importDetails, exports,
    resolved_imports: resolution.resolved_imports, unresolved_imports: [...resolution.unresolved_imports, ...dynamicImports],
    resolution_configs: resolution.resolution_configs,
    top_level_hash: hash(source.statements.filter(n => !(ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n)
      || ts.isInterfaceDeclaration(n) || ts.isTypeAliasDeclaration(n) || ts.isEnumDeclaration(n)
      || ts.isVariableStatement(n))).map(canonical).join('|')),
    symbols: symbols.sort((a, b) => a.line - b.line || a.name.localeCompare(b.name)),
    line_end: (text.match(/\n/g) || []).length + (text.length && !text.endsWith('\n') ? 1 : 0),
    content_hash: hash(raw), line_quality: 'ast', parser: {name: 'typescript', version: ts.version}}, diagnostics: resolution.diagnostics};
}

try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  let ts;
  try { ts = loadCompiler(input); }
  catch (error) { process.stdout.write(JSON.stringify({unavailable: error.message})); process.exit(2); }
  const files = [], diagnostics = [];
  const resolveImports = resolver(ts, input);
  for (const relative of input.files) {
    try {
      const result = analyze(ts, input.root, relative, resolveImports);
      if (result.file) files.push(result.file);
      diagnostics.push(...result.diagnostics);
    } catch (error) { diagnostics.push({path: relative, reason: error.message}); }
  }
  process.stdout.write(JSON.stringify({files, diagnostics, parser_version: ts.version}));
} catch (error) {
  process.stderr.write(error.stack || String(error)); process.exit(1);
}
