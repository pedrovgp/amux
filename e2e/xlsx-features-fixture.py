#!/usr/bin/env python3
"""A workbook with a filter, data validation and conditional formats, written
as raw XML the way Excel writes them (no openpyxl in CI).

    xlsx-features-fixture.py <out.xlsx>
"""
import sys
import zipfile

out = sys.argv[1]
rows = ['<row r="1"><c r="A1" t="inlineStr"><is><t>Invoice</t></is></c><c r="B1" t="inlineStr"><is><t>Amount</t></is></c><c r="C1" t="inlineStr"><is><t>Status</t></is></c></row>']
for i in range(1, 21):
    r = i + 1
    status = "Open" if i % 2 else "Paid"
    rows.append(f'<row r="{r}"><c r="A{r}" t="inlineStr"><is><t>INV-{i}</t></is></c><c r="B{r}"><v>{i * 37.5}</v></c><c r="C{r}" t="inlineStr"><is><t>{status}</t></is></c></row>')
sheet = (
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<dimension ref="A1:C21"/><sheetData>' + "".join(rows) + '</sheetData>'
    '<autoFilter ref="A1:C21"/>'
    '<conditionalFormatting sqref="B2:B21">'
    '<cfRule type="cellIs" dxfId="0" priority="1" operator="greaterThan"><formula>500</formula></cfRule>'
    '<cfRule type="colorScale" priority="2"><colorScale><cfvo type="min"/><cfvo type="max"/><color rgb="FFF8696B"/><color rgb="FF63BE7B"/></colorScale></cfRule>'
    '</conditionalFormatting>'
    '<conditionalFormatting sqref="C2:C21"><cfRule type="containsText" dxfId="1" priority="3" operator="containsText" text="Paid"><formula>NOT(ISERROR(SEARCH("Paid",C2)))</formula></cfRule></conditionalFormatting>'
    '<conditionalFormatting sqref="A2:A21"><cfRule type="expression" dxfId="0" priority="4"><formula>$B2&gt;300</formula></cfRule></conditionalFormatting>'
    '<dataValidations count="2">'
    '<dataValidation type="list" allowBlank="1" showErrorMessage="1" errorTitle="Status" error="Pick one" sqref="C2:C21"><formula1>"Open,Paid"</formula1></dataValidation>'
    '<dataValidation type="decimal" operator="between" sqref="B2:B21"><formula1>0</formula1><formula2>1000</formula2></dataValidation>'
    '</dataValidations>'
    '</worksheet>'
)
styles = (
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    '<dxfs count="2"><dxf><font><color rgb="FF9C0006"/></font><fill><patternFill><bgColor rgb="FFFFC7CE"/></patternFill></fill></dxf>'
    '<dxf><font><b/></font></dxf></dxfs>'
    '</styleSheet>'
)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as book:
    book.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    book.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    book.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets><definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">Data!$A$1:$C$21</definedName></definedNames></workbook>')
    book.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    book.writestr("xl/styles.xml", styles)
    book.writestr("xl/worksheets/sheet1.xml", sheet)
