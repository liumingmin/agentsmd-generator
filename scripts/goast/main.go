// goast: Go 项目 AST 静态分析器。
// 遍历项目内所有 .go 文件，输出 JSON 格式的代码索引：
// 包结构、文件清单、类型/函数/方法的名称、起止行号、导出性、文档注释摘要。
// 用法: goast -root <项目根目录> [-out <输出json路径>] [-test]
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/scanner"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Symbol 表示一个顶层符号（类型/函数/方法）。
type Symbol struct {
	ID            string  `json:"id"`
	ContentHash   string  `json:"content_hash"`
	BodyHash      string  `json:"body_hash,omitempty"`
	SignatureHash string  `json:"signature_hash,omitempty"`
	Signature     string  `json:"signature,omitempty"`
	Name          string  `json:"name"`              // 符号名，方法为 Recv.Name
	Kind          string  `json:"kind"`              // func | method | struct | interface | type | const | var
	Exported      bool    `json:"exported"`          // 是否导出
	Line          int     `json:"line"`              // 起始行
	EndLine       int     `json:"end_line"`          // 结束行
	Doc           string  `json:"doc,omitempty"`     // 文档注释首行
	Recv          string  `json:"recv,omitempty"`    // 方法的接收者类型
	Params        string  `json:"params,omitempty"`  // 参数摘要
	Results       string  `json:"results,omitempty"` // 返回值摘要
	Fields        []Field `json:"fields,omitempty"`  // struct 字段 / interface 方法（仅 kind=struct/interface 时输出）
}

// Field 表示结构体字段或接口方法签名。
type Field struct {
	Name     string `json:"name"`               // 字段/方法名；嵌入字段为类型名
	Type     string `json:"type,omitempty"`     // 字段类型 / 方法签名摘要
	Line     int    `json:"line"`               // 所在行
	Tag      string `json:"tag,omitempty"`      // 结构体 tag（去掉反引号）
	Embedded bool   `json:"embedded,omitempty"` // 是否嵌入字段
	Doc      string `json:"doc,omitempty"`      // 注释首行
}

// FileInfo 表示一个 Go 文件。
type FileInfo struct {
	TopLevelHash     string   `json:"top_level_hash"`
	Category         string   `json:"category"`
	LineQuality      string   `json:"line_quality"`
	ContentHash      string   `json:"content_hash"`
	Generated        bool     `json:"generated,omitempty"`
	BuildConstraints []string `json:"build_constraints,omitempty"`
	Path             string   `json:"path"`     // 相对项目根的路径（正斜杠）
	Package          string   `json:"package"`  // 包名
	LineEnd          int      `json:"line_end"` // 文件总行数
	Imports          []string `json:"imports"`  // 导入路径
	Symbols          []Symbol `json:"symbols"`  // 顶层符号
	IsTest           bool     `json:"is_test"`  // 是否测试文件
}

// PkgInfo 表示一个包（目录）。
type PkgInfo struct {
	Dir     string   `json:"dir"`     // 相对项目根的目录
	Package string   `json:"package"` // 包名
	Files   []string `json:"files"`   // 该包内的文件
}

// Route 表示一个 HTTP 路由注册（如 admin.POST("/path", Handler)）。
type Route struct {
	Receiver   string   `json:"receiver"`
	Handlers   []string `json:"handlers"`
	Confidence string   `json:"confidence"`
	Method     string   `json:"method"`  // HTTP 方法（POST/GET/...）
	Path       string   `json:"path"`    // 路由路径
	Handler    string   `json:"handler"` // 处理函数（尽量带包名）
	File       string   `json:"file"`    // 所在文件
	Line       int      `json:"line"`    // 所在行
}

// CronJob 表示一个 cron 表达式（来自字符串字面量或注释）。
type CronJob struct {
	Source     string `json:"source"`
	Confidence string `json:"confidence"`
	Expr       string `json:"expr"` // cron 表达式
	File       string `json:"file"`
	Line       int    `json:"line"`
}

// Diagnostic records a file excluded because it could not be parsed.
type Diagnostic struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

// Report 是分析结果。
type Report struct {
	LocalModules  map[string]string `json:"local_modules"`
	Scope         json.RawMessage   `json:"scope"`
	SchemaVersion int               `json:"schema_version"`
	Diagnostics   []Diagnostic      `json:"diagnostics"`
	Root          string            `json:"root"`
	Packages      []PkgInfo         `json:"packages"`
	Files         []FileInfo        `json:"files"`
	Routes        []Route           `json:"routes,omitempty"`
	Crons         []CronJob         `json:"crons,omitempty"`
	Stats         Stats             `json:"stats"`
}

// Stats 统计信息。
type CategoryStats struct {
	FileCount   int `json:"file_count"`
	SymbolCount int `json:"symbol_count"`
}

type Stats struct {
	ByCategory   map[string]CategoryStats `json:"by_category"`
	FileCount    int                      `json:"file_count"`
	PackageCount int                      `json:"package_count"`
	SymbolCount  int                      `json:"symbol_count"`
}

var skipDirs = map[string]bool{
	"vendor": true, ".git": true, "node_modules": true,
	".workbuddy": true, "testdata": false, // testdata 保留
}

func main() {
	root := flag.String("root", ".", "项目根目录")
	out := flag.String("out", "", "输出 JSON 文件路径（缺省输出到 stdout）")
	selectionPath := flag.String("selection", "", "shared selection.json from scanpolicy.py")
	includeTest := flag.Bool("test", true, "是否包含 _test.go 文件")
	flag.Parse()

	absRoot, err := filepath.Abs(*root)
	if err != nil {
		fatal(err)
	}

	absRoot, err = filepath.EvalSymlinks(absRoot)
	if err != nil {
		fatal(err)
	}
	fset := token.NewFileSet()
	report := &Report{SchemaVersion: 3, Root: filepath.ToSlash(absRoot), Files: []FileInfo{}, Packages: []PkgInfo{}, Diagnostics: []Diagnostic{}}
	pkgMap := map[string]*PkgInfo{}

	var selected []string
	if *selectionPath != "" {
		var manifest struct {
			Root  string          `json:"root"`
			Files []string        `json:"files"`
			Scope json.RawMessage `json:"scope"`
		}
		data, readErr := os.ReadFile(*selectionPath)
		if readErr != nil {
			fatal(readErr)
		}
		if err := json.Unmarshal(data, &manifest); err != nil {
			fatal(err)
		}
		manifestRoot, _ := filepath.Abs(manifest.Root)
		manifestRoot, err = filepath.EvalSymlinks(manifestRoot)
		if err != nil {
			fatal(err)
		}
		if filepath.Clean(manifestRoot) != filepath.Clean(absRoot) {
			fatal(fmt.Errorf("selection root mismatch"))
		}
		sort.Strings(manifest.Files)
		var scope struct {
			SelectionHash string `json:"selection_hash"`
		}
		if err := json.Unmarshal(manifest.Scope, &scope); err != nil {
			fatal(err)
		}
		if scope.SelectionHash != fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(manifest.Files, "\n")))) {
			fatal(fmt.Errorf("selection hash mismatch"))
		}
		seen := map[string]bool{}
		for _, rel := range manifest.Files {
			clean := filepath.Clean(filepath.FromSlash(rel))
			if filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) || seen[rel] {
				fatal(fmt.Errorf("unsafe/duplicate selection path: %s", rel))
			}
			seen[rel] = true
			path := filepath.Join(absRoot, clean)
			resolved, e := filepath.EvalSymlinks(path)
			if e != nil {
				fatal(e)
			}
			if resolved != path {
				fatal(fmt.Errorf("symlink selection path: %s", rel))
			}
			if strings.HasSuffix(rel, ".go") {
				selected = append(selected, path)
			}
		}
		report.Scope = manifest.Scope
	} else {
		report.Scope = json.RawMessage(`{"policy":{"mode":"legacy-walk"},"gitignore_applied":false}`)
		err = filepath.Walk(absRoot, func(path string, info os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			if info.IsDir() {
				if path != absRoot && (skipDirs[info.Name()] || strings.HasPrefix(info.Name(), ".")) {
					return filepath.SkipDir
				}
			} else if info.Mode()&os.ModeSymlink == 0 && strings.HasSuffix(info.Name(), ".go") {
				selected = append(selected, path)
			}
			return nil
		})
		if err != nil {
			fatal(err)
		}
	}
	process := func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if path != absRoot && skipDirs[info.Name()] {
				return filepath.SkipDir
			}
			if path != absRoot && strings.HasPrefix(info.Name(), ".") {
				return filepath.SkipDir
			}
			return nil
		}
		if info.Mode()&os.ModeSymlink != 0 || !strings.HasSuffix(info.Name(), ".go") {
			return nil
		}
		relTest, _ := filepath.Rel(absRoot, path)
		isTest := isTestPath(filepath.ToSlash(relTest))
		if isTest && !*includeTest {
			return nil
		}

		rel, _ := filepath.Rel(absRoot, path)
		rel = filepath.ToSlash(rel)

		source, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		f, err := parser.ParseFile(fset, path, source, parser.ParseComments|parser.SkipObjectResolution)
		if err != nil {
			fmt.Fprintf(os.Stderr, "warn: parse %s: %v\n", rel, err)
			report.Diagnostics = append(report.Diagnostics, Diagnostic{Path: rel, Reason: err.Error()})
			return nil
		}

		fi := FileInfo{
			Path:        rel,
			Package:     f.Name.Name,
			LineEnd:     physicalLines(source),
			ContentHash: fmt.Sprintf("%x", sha256.Sum256(source)),
			Generated:   ast.IsGenerated(f) || generatedHeader.Match(source[:min(len(source), 8192)]),
			IsTest:      isTest,
			LineQuality: "ast",
			Category:    "production",
		}
		if fi.Generated {
			fi.Category = "generated"
		}
		if fi.IsTest {
			fi.Category = "test"
		}
		for _, group := range f.Comments {
			for _, comment := range group.List {
				if strings.HasPrefix(comment.Text, "//go:build ") || strings.HasPrefix(comment.Text, "// +build ") {
					fi.BuildConstraints = append(fi.BuildConstraints, comment.Text)
				}
			}
		}
		for _, imp := range f.Imports {
			fi.Imports = append(fi.Imports, unquote(imp.Path.Value))
		}

		fi.TopLevelHash = fmt.Sprintf("%x", sha256.Sum256([]byte(fi.Package+"\n"+strings.Join(fi.Imports, "\n"))))

		// 提取路由注册（xxx.POST("/path", handler)）与 cron 表达式
		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.CallExpr:
				if r, ok := extractRoute(fset, node, rel); ok {
					report.Routes = append(report.Routes, r)
				}
			case *ast.BasicLit:
				if node.Kind == token.STRING {
					if expr, ok := parseCron(unquote(node.Value)); ok {
						report.Crons = append(report.Crons, CronJob{Expr: expr, File: rel, Line: fset.Position(node.Pos()).Line, Source: "string", Confidence: "candidate"})
					}
				}
			case *ast.Comment:
				if expr, ok := parseCron(node.Text); ok {
					report.Crons = append(report.Crons, CronJob{Expr: expr, File: rel, Line: fset.Position(node.Pos()).Line, Source: "comment", Confidence: "candidate"})
				}
			}
			return true
		})

		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				s := Symbol{
					Kind:          "func",
					Signature:     exprString(d.Type),
					ContentHash:   fingerprint(d),
					BodyHash:      bodyFingerprint(d.Body),
					SignatureHash: fingerprint(d.Type),
					Name:          d.Name.Name,
					Exported:      ast.IsExported(d.Name.Name),
					Line:          fset.Position(d.Pos()).Line,
					EndLine:       fset.Position(d.End()).Line,
					Doc:           firstLine(d.Doc),
					Params:        fieldListSummary(d.Type.Params),
					Results:       fieldListSummary(d.Type.Results),
				}
				if d.Recv != nil && len(d.Recv.List) > 0 {
					s.Kind = "method"
					s.SignatureHash = fmt.Sprintf("%x", sha256.Sum256([]byte(s.SignatureHash+fieldListSummary(d.Recv))))
					s.Recv = recvTypeName(d.Recv.List[0].Type)
					s.Name = s.Recv + "." + d.Name.Name
				}
				fi.Symbols = append(fi.Symbols, s)
			case *ast.GenDecl:
				for _, spec := range d.Specs {
					switch sp := spec.(type) {
					case *ast.TypeSpec:
						kind := "type"
						var fields []Field
						switch t := sp.Type.(type) {
						case *ast.StructType:
							kind = "struct"
							fields = structFields(fset, t)
						case *ast.InterfaceType:
							kind = "interface"
							fields = interfaceMethods(fset, t)
						}
						fi.Symbols = append(fi.Symbols, Symbol{
							Kind:          kind,
							Name:          sp.Name.Name,
							Exported:      ast.IsExported(sp.Name.Name),
							Line:          fset.Position(sp.Pos()).Line,
							EndLine:       fset.Position(sp.End()).Line,
							Doc:           specDoc(sp.Doc, d.Doc),
							Signature:     nodeString(sp),
							ContentHash:   fingerprint(sp),
							SignatureHash: fingerprint(sp),
							Fields:        fields,
						})
					case *ast.ValueSpec:
						kind := "var"
						if d.Tok == token.CONST {
							kind = "const"
						}
						for _, name := range sp.Names {
							fi.Symbols = append(fi.Symbols, Symbol{
								Kind:          kind,
								ContentHash:   fingerprint(sp),
								SignatureHash: fingerprint(sp),
								Name:          name.Name,
								Exported:      ast.IsExported(name.Name),
								Line:          fset.Position(sp.Pos()).Line,
								EndLine:       fset.Position(sp.End()).Line,
								Doc:           specDoc(sp.Doc, d.Doc),
							})
						}
					}
				}
			}
		}

		ordinals := map[string]int{}
		for i := range fi.Symbols {
			key := fi.Symbols[i].Kind + ":" + fi.Symbols[i].Name
			fi.Symbols[i].ID = fmt.Sprintf("%s#%d", key, ordinals[key])
			ordinals[key]++
		}
		report.Files = append(report.Files, fi)
		report.Stats.SymbolCount += len(fi.Symbols)

		dir := filepath.ToSlash(filepath.Dir(rel))
		if dir == "." {
			dir = ""
		}
		key := dir + "/" + fi.Package
		p, ok := pkgMap[key]
		if !ok {
			p = &PkgInfo{Dir: dir, Package: fi.Package}
			pkgMap[key] = p
		}
		p.Files = append(p.Files, rel)
		return nil
	}
	for _, path := range selected {
		info, statErr := os.Lstat(path)
		if err := process(path, info, statErr); err != nil {
			fatal(err)
		}
	}

	report.Stats.ByCategory = map[string]CategoryStats{"production": {}, "test": {}, "generated": {}}
	for _, file := range report.Files {
		count := report.Stats.ByCategory[file.Category]
		count.FileCount++
		count.SymbolCount += len(file.Symbols)
		report.Stats.ByCategory[file.Category] = count
	}
	for _, p := range pkgMap {
		sort.Strings(p.Files)
		report.Packages = append(report.Packages, *p)
	}
	sort.Slice(report.Packages, func(i, j int) bool {
		if report.Packages[i].Dir == report.Packages[j].Dir {
			return report.Packages[i].Package < report.Packages[j].Package
		}
		return report.Packages[i].Dir < report.Packages[j].Dir
	})
	sort.Slice(report.Files, func(i, j int) bool { return report.Files[i].Path < report.Files[j].Path })
	report.Stats.FileCount = len(report.Files)
	report.Stats.PackageCount = len(report.Packages)
	report.LocalModules = localModulePaths(absRoot, report.Packages)

	data, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		fatal(err)
	}
	if *out == "" {
		fmt.Println(string(data))
		return
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		fatal(err)
	}
	if err := os.WriteFile(*out, data, 0o644); err != nil {
		fatal(err)
	}
	fmt.Fprintf(os.Stderr, "goast: %d files, %d packages, %d symbols -> %s\n",
		report.Stats.FileCount, report.Stats.PackageCount, report.Stats.SymbolCount, *out)
}

func firstLine(cg *ast.CommentGroup) string {
	if cg == nil {
		return ""
	}
	text := strings.TrimSpace(cg.Text())
	if i := strings.IndexByte(text, '\n'); i >= 0 {
		text = text[:i]
	}
	const maxLen = 120
	if len([]rune(text)) > maxLen {
		text = string([]rune(text)[:maxLen]) + "..."
	}
	return text
}

// httpMethods 是常见 web 框架的路由注册方法名。
var httpMethods = map[string]bool{
	"GET": true, "POST": true, "PUT": true, "DELETE": true,
	"PATCH": true, "HEAD": true, "OPTIONS": true, "Any": true, "Handle": true,
}

// extractRoute 识别 xxx.POST("/path", handler) 形式的路由注册调用。
func extractRoute(fset *token.FileSet, call *ast.CallExpr, file string) (Route, bool) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || !httpMethods[sel.Sel.Name] {
		return Route{}, false
	}
	if len(call.Args) < 2 {
		return Route{}, false
	}
	pathLit, ok := call.Args[0].(*ast.BasicLit)
	if !ok || pathLit.Kind != token.STRING {
		return Route{}, false
	}
	path := unquote(pathLit.Value)
	if !strings.HasPrefix(path, "/") {
		return Route{}, false
	}
	handlers := []string{}
	for _, arg := range call.Args[1:] {
		handlers = append(handlers, exprString(arg))
	}
	return Route{
		Receiver:   exprString(sel.X),
		Handlers:   handlers,
		Confidence: "candidate",
		Method:     strings.ToUpper(sel.Sel.Name),
		Path:       path,
		Handler:    handlers[len(handlers)-1],
		File:       file,
		Line:       fset.Position(call.Pos()).Line,
	}, true
}

// parseCron identifies whole candidate expressions; it does not validate registration or field ranges.
func parseCron(text string) (string, bool) {
	text = strings.TrimSpace(strings.TrimLeft(text, "/# "))
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return "", false
	}
	if fields[0] == "@every" && len(fields) == 2 {
		if duration, err := time.ParseDuration(fields[1]); err == nil && duration > 0 {
			return strings.Join(fields, " "), true
		}
	}
	for _, descriptor := range []string{"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly", "@reboot"} {
		if fields[0] == descriptor && len(fields) == 1 {
			return fields[0], true
		}
	}
	if len(fields) < 5 || len(fields) > 7 {
		return "", false
	}
	cand := fields // Preserve seconds/year fields; this is a candidate, not validation.
	for _, f := range cand {
		if !cronFieldOK(f) {
			return "", false
		}
	}
	return strings.Join(cand, " "), true
}

func cronFieldOK(f string) bool {
	if f == "" {
		return false
	}
	for _, r := range f {
		if (r < '0' || r > '9') && r != '*' && r != '/' && r != ',' && r != '-' && r != '?' {
			return false
		}
	}
	return true
}

// structFields 提取结构体字段（含行号、tag、嵌入标记、注释）。
func structFields(fset *token.FileSet, st *ast.StructType) []Field {
	if st.Fields == nil {
		return nil
	}
	var out []Field
	for _, f := range st.Fields.List {
		line := fset.Position(f.Pos()).Line
		tag := ""
		if f.Tag != nil {
			tag = strings.Trim(f.Tag.Value, "`")
		}
		doc := firstLine(f.Doc)
		if doc == "" {
			doc = firstLine(f.Comment)
		}
		if len(f.Names) == 0 {
			// 嵌入字段
			out = append(out, Field{
				Name:     exprString(f.Type),
				Line:     line,
				Tag:      tag,
				Embedded: true,
				Doc:      doc,
			})
			continue
		}
		typeName := exprString(f.Type)
		for _, n := range f.Names {
			out = append(out, Field{
				Name: n.Name,
				Type: typeName,
				Line: line,
				Tag:  tag,
				Doc:  doc,
			})
		}
	}
	return out
}

// interfaceMethods 提取接口方法签名与嵌入接口。
func interfaceMethods(fset *token.FileSet, it *ast.InterfaceType) []Field {
	if it.Methods == nil {
		return nil
	}
	var out []Field
	for _, m := range it.Methods.List {
		line := fset.Position(m.Pos()).Line
		doc := firstLine(m.Doc)
		if len(m.Names) == 0 {
			// 嵌入接口
			out = append(out, Field{
				Name:     exprString(m.Type),
				Line:     line,
				Embedded: true,
				Doc:      doc,
			})
			continue
		}
		sig := ""
		if ft, ok := m.Type.(*ast.FuncType); ok {
			sig = "(" + fieldListSummary(ft.Params) + ")"
			if r := fieldListSummary(ft.Results); r != "" {
				sig += " (" + r + ")"
			}
		}
		for _, n := range m.Names {
			out = append(out, Field{
				Name: n.Name,
				Type: sig,
				Line: line,
				Doc:  doc,
			})
		}
	}
	return out
}

func recvTypeName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return recvTypeName(t.X)
	case *ast.Ident:
		return t.Name
	case *ast.IndexExpr: // 泛型
		return recvTypeName(t.X)
	case *ast.IndexListExpr:
		return recvTypeName(t.X)
	}
	return "?"
}

func fieldListSummary(fl *ast.FieldList) string {
	if fl == nil || len(fl.List) == 0 {
		return ""
	}
	var parts []string
	for _, field := range fl.List {
		typeName := exprString(field.Type)
		if len(field.Names) == 0 {
			parts = append(parts, typeName)
		} else {
			var names []string
			for _, n := range field.Names {
				names = append(names, n.Name)
			}
			parts = append(parts, strings.Join(names, ",")+" "+typeName)
		}
	}
	return strings.Join(parts, ", ")
}

func exprString(expr ast.Expr) string {
	return nodeString(expr)
}

func nodeString(node ast.Node) string {
	var out bytes.Buffer
	if err := printer.Fprint(&out, token.NewFileSet(), node); err != nil {
		return "?"
	}
	return out.String()
}

func specDoc(primary, fallback *ast.CommentGroup) string {
	if primary != nil {
		return firstLine(primary)
	}
	return firstLine(fallback)
}

func unquote(value string) string {
	decoded, err := strconv.Unquote(value)
	if err != nil {
		return value
	}
	return decoded
}

func physicalLines(source []byte) int {
	if len(source) == 0 {
		return 0
	}
	count := bytes.Count(source, []byte("\n"))
	if source[len(source)-1] != '\n' {
		count++
	}
	return count
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "goast error:", err)
	os.Exit(1)
}

// Hash lexical tokens from the AST printer, excluding comments and positions.
func fingerprint(node ast.Node) string {
	if node == nil {
		return ""
	}
	text := nodeString(node)
	var scan scanner.Scanner
	fset := token.NewFileSet()
	scan.Init(fset.AddFile("", -1, len(text)), []byte(text), nil, 0)
	var normalized strings.Builder
	for {
		_, tok, lit := scan.Scan()
		if tok == token.EOF {
			break
		}
		if tok == token.SEMICOLON {
			lit = ";"
		}
		fmt.Fprintf(&normalized, "%d:%q|", tok, lit)
	}
	return fmt.Sprintf("%x", sha256.Sum256([]byte(normalized.String())))
}

func bodyFingerprint(body *ast.BlockStmt) string {
	if body == nil {
		return ""
	}
	return fingerprint(body)
}

var generatedHeader = regexp.MustCompile(`(?im)^.{0,10}(?:Code generated\b[^\n]*DO NOT EDIT|@generated\b)`)

func isTestPath(path string) bool {
	parts := strings.Split(path, "/")
	for _, part := range parts[:len(parts)-1] {
		switch strings.ToLower(part) {
		case "test", "tests", "__tests__", "testdata":
			return true
		}
	}
	name := parts[len(parts)-1]
	return strings.HasPrefix(name, "test_") || strings.HasSuffix(name, "_test.go") || strings.Contains(name, ".test.") || strings.Contains(name, ".spec.")
}

// Save module identities with the snapshot so old import edges survive module renames.
func localModulePaths(root string, packages []PkgInfo) map[string]string {
	result := map[string]string{}
	checked := map[string]bool{}
	for _, pkg := range packages {
		current := filepath.Join(root, filepath.FromSlash(pkg.Dir))
		for {
			rel, err := filepath.Rel(root, current)
			if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
				break
			}
			if checked[current] {
				break
			}
			checked[current] = true
			if data, err := os.ReadFile(filepath.Join(current, "go.mod")); err == nil {
				for _, line := range strings.Split(string(data), "\n") {
					fields := strings.Fields(strings.SplitN(line, "//", 2)[0])
					if len(fields) >= 2 && fields[0] == "module" {
						result[filepath.ToSlash(rel)] = strings.Trim(fields[1], "\"")
						break
					}
				}
			}
			if current == root {
				break
			}
			current = filepath.Dir(current)
		}
	}
	return result
}
