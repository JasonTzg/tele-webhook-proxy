import os
import io
import requests
from weasyprint import HTML
from pypdf import PdfWriter, PdfReader
from jinja2 import Environment, FileSystemLoader

def generate_invoice_pdf(invoice_data: dict, output_path: str):
    """
    Generate an invoice PDF from JSON data using Jinja2 and WeasyPrint,
    then append the payment.pdf.
    """
    # 1. Prepare data variables
    items = invoice_data.get('items', [])
    items_with_amounts = []
    total = 0.0
    
    for item in items:
        qty = float(item.get('qty', 0))
        unit_price = float(item.get('unit_price', 0))
        amount = qty * unit_price
        item['amount'] = amount
        items_with_amounts.append(item)
        total += amount

    # Setup Jinja2 environment
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_dir = os.path.join(base_dir, 'templates')
    assets_dir = os.path.join(base_dir, 'assets')
    
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template('invoice.html')
    
    # Path to logo - can be a URL or local path.
    # Instead use online URL for logo to prevent traces of local file paths in PDF metadata
    logo_uri = os.getenv('logo_url', '')

    # Pull co_reg_no from env variable
    co_reg_no = os.getenv('co_reg_no', '')
    invoice_data['co_reg_no'] = co_reg_no

    # Pull company data from env variables
    company_name = os.getenv('company_name', 'Your Company Name')
    company_address_line1 = os.getenv('company_address_line1', 'Your Company Address Line 1')
    company_address_line2 = os.getenv('company_address_line2', 'Your Company Address Line 2')
    company_address_line3 = os.getenv('company_address_line3', 'Your Company Address Line 3')
    
    # Render HTML
    html_out = template.render(
        invoice=invoice_data,
        items_with_amounts=items_with_amounts,
        total=total,
        logo_uri=logo_uri,
        company_name=company_name,
        company_address_line1=company_address_line1,
        company_address_line2=company_address_line2,
        company_address_line3=company_address_line3
    )
    
    # 2. Render main invoice PDF in memory
    pdf_bytes = HTML(string=html_out, base_url=base_dir).write_pdf()
    
    # 3. Append payment.pdf using PyPDF with pdf found online. If not found, DO NOT USE anything.
    payment_pdf_url = os.getenv('payment_pdf_url', '').strip()
    
    writer = PdfWriter()
    
    # Add invoice pages
    invoice_reader = PdfReader(io.BytesIO(pdf_bytes))
    for page in invoice_reader.pages:
        writer.add_page(page)
        
    if payment_pdf_url:
        try:
            response = requests.get(payment_pdf_url, timeout=12)
            response.raise_for_status()
            payment_reader = PdfReader(io.BytesIO(response.content))
            for page in payment_reader.pages:
                writer.add_page(page)
        except Exception as e:
            print(f"Warning: Could not merge payment PDF from URL {payment_pdf_url}: {e}")

    # Write the final PDF to disk
    with open(output_path, "wb") as f_out:
        writer.write(f_out)
        
    return output_path
