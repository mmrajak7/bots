import os

def main():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sl_comparison_analysis.py')
    print(f'Writing to: {p}')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(get_script())
    print('Done!')

if __name__ == '__main__':
    main()
