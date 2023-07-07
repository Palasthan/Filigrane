# watermark
Python script to add a watermark or logo to images

### Requirements
Pillow:
```
pip install pillow
```

[Pillow Docs](https://python-pillow.github.io/)

### Usage
This script allows you to add a watermark or logo to images in a specified folder. The script takes three arguments:

1. The folder with the images you want to watermark
2. The path of the logo to add
3. The float value for the alpha value on the logo
4. The position you want to place the logo (optional)

The final value for the alpha float will depend on the current value for each pixel, will be proportional.
Must introduce a number between 0 and 1 

> will default to 0.5

```
python watermark.py  './images' 'logo.png' 0.4
