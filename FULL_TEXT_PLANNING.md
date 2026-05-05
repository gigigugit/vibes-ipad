I want to work on getting text from the page to display in an overlay that is reliable making it possible to see what is written in the various elements on the page without to scroll or click anything. 

I do not want to find all the selectors for everything. The workflow I would choose is that we would:
1. function that finds all visible text, and then finds the elements and their positions on the page
2. create an overlay that displays the text in a way that corresponds to its position on the page
3. Ensure that the overlay is responsive and updates as the user scrolls or interacts with the page

in broad strokes, the manner of implementation would be:
1. Use JavaScript to traverse the DOM and identify all visible text elements. This can be done using methods like `document.querySelectorAll` to select elements and checking their visibility with `getComputedStyle`.
2. For each visible text element, retrieve its text content and calculate its position on the page using methods like `getBoundingClientRect()`. This will give you the coordinates and dimensions of the element, which can be used to position the overlay correctly.
3. Create an overlay element (e.g., a div) that will display the text. This overlay should be styled to be semi-transparent and positioned absolutely on the page. You can use CSS to ensure that it does not interfere with user interactions on the underlying page.
4. Implement event listeners for scroll and resize events to ensure that the overlay updates its position and content as the user interacts with the page. This can be done by recalculating the positions of the text elements and updating the overlay accordingly.**